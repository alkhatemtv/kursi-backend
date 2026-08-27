"""Layout freeze + inventory materialisation (spec 2 and 3).

    "Freezing happens automatically the first time a performance generates
     inventory from the version (transactionally, in code) - never manually
     unfrozen."

ONE TRANSACTION, TWO IRREVERSIBLE THINGS
----------------------------------------
Freezing a layout version and materialising `performance_seats` from it are the
same event. If the freeze committed and the materialisation failed, a version
would be sealed against edits with nothing to show for it; if the seats
committed and the freeze failed, live inventory would reference a document that
someone could still edit underneath it. So both happen inside one
`unit_of_work`, and the layout is validated *completely* before either is
attempted (see `layout.parse_layout`).

IDEMPOTENCE
-----------
Re-publishing must be a no-op for seats, not a duplicate-key error. The
materialiser reads the set of `seat_uid`s that already exist for the
performance and inserts only the difference, so
`UNIQUE(performance_id, seat_uid)` never fires in normal flow - it stays what it
is meant to be, a backstop against a bug rather than a control-flow mechanism.
Prices, by contrast, ARE updated on re-publish: repricing a performance is a
legitimate operator action, and the layout (frozen) never holds a price.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.engine_models import (
    EngineEvent,
    LayoutVersion,
    Performance,
    PerformanceCategory,
    PerformanceSeat,
)
from app.engine_services import clock
from app.engine_services.audit import (
    ACTION_LAYOUT_FROZEN,
    ACTION_PERFORMANCE_PUBLISHED,
    record_audit,
)
from app.engine_services.errors import (
    ConcurrentPublish,
    NotFound,
    ValidationError,
)
from app.engine_services.layout import ParsedLayout, parse_layout
from app.engine_services.pricing import DEFAULT_CURRENCY, normalize_prices
from app.engine_services.uow import unit_of_work


@dataclass
class PublishResult:
    performance_id: int
    layout_version_id: int
    froze_layout: bool
    seats_created: int
    seats_existing: int
    categories_created: int
    categories_updated: int
    status: str

    @property
    def seats_total(self) -> int:
        return self.seats_created + self.seats_existing


def _resolve_id(value: Any) -> int:
    """Accept an ORM object or a bare id, so callers can pass whichever they hold."""
    return value.id if hasattr(value, "id") else int(value)


def load_publish_context(
    session: Session, performance_id: int
) -> tuple[Performance, LayoutVersion, int]:
    """(performance, its layout version, owning organization id)."""
    performance = session.get(Performance, performance_id)
    if performance is None:
        raise NotFound(f"performance {performance_id} does not exist")

    version = session.get(LayoutVersion, performance.layout_version_id)
    if version is None:  # pragma: no cover - FK RESTRICT makes this unreachable
        raise NotFound(
            f"performance {performance_id} references missing layout version "
            f"{performance.layout_version_id}"
        )

    event = session.get(EngineEvent, performance.event_id)
    if event is None:  # pragma: no cover - FK RESTRICT makes this unreachable
        raise NotFound(f"performance {performance_id} references missing event")

    return performance, version, event.organization_id


def _validate_prices(
    parsed: ParsedLayout, prices: dict[str, tuple[int, str]]
) -> None:
    """Every category a seat actually uses must be priced; no stray keys."""
    referenced = parsed.referenced_category_keys
    missing = sorted(referenced - set(prices))
    unknown = sorted(set(prices) - parsed.category_keys - referenced)

    problems = []
    if missing:
        problems.append(f"no price given for category key(s): {', '.join(missing)}")
    if unknown:
        problems.append(
            f"price given for category key(s) the layout does not define: "
            f"{', '.join(unknown)}"
        )
    if problems:
        raise ValidationError(
            "; ".join(problems), missing=missing, unknown=unknown
        )


def _freeze(session: Session, version: LayoutVersion, moment: datetime) -> bool:
    """draft -> frozen. Returns whether this call did the freezing.

    The database rejects any later edit of `layout_data` and any move back to
    draft (Phase 1a trigger); this is only the transition itself.
    """
    if version.status == "frozen":
        return False
    version.status = "frozen"
    version.frozen_at = moment
    session.flush()
    return True


def _sync_categories(
    session: Session,
    performance_id: int,
    parsed: ParsedLayout,
    prices: dict[str, tuple[int, str]],
) -> tuple[int, int]:
    """Create/update `performance_categories`. Returns (created, updated)."""
    existing = {
        row.category_key: row
        for row in session.execute(
            select(PerformanceCategory).where(
                PerformanceCategory.performance_id == performance_id
            )
        ).scalars()
    }
    definitions = {c.category_key: c for c in parsed.categories}

    created = updated = 0
    for key, (amount_minor, currency) in prices.items():
        definition = definitions.get(key)
        name = definition.name if definition else key
        name_ar = definition.name_ar if definition else None
        color = definition.color if definition else None

        row = existing.get(key)
        if row is None:
            session.add(
                PerformanceCategory(
                    performance_id=performance_id,
                    category_key=key,
                    name=name,
                    name_ar=name_ar,
                    color=color,
                    amount_minor=amount_minor,
                    currency=currency,
                )
            )
            created += 1
            continue

        changed = (
            row.amount_minor != amount_minor
            or row.currency != currency
            or row.name != name
            or row.name_ar != name_ar
            or row.color != color
        )
        if changed:
            row.amount_minor = amount_minor
            row.currency = currency
            row.name = name
            row.name_ar = name_ar
            row.color = color
            updated += 1

    session.flush()
    return created, updated


def _materialize_seats(
    session: Session, performance_id: int, parsed: ParsedLayout
) -> tuple[int, int]:
    """Insert the seats that are not there yet. Returns (created, already_present)."""
    existing_uids = set(
        session.execute(
            select(PerformanceSeat.seat_uid).where(
                PerformanceSeat.performance_id == performance_id
            )
        ).scalars()
    )

    to_create = [seat for seat in parsed.seats if seat.seat_uid not in existing_uids]
    session.add_all(
        [
            PerformanceSeat(
                performance_id=performance_id,
                seat_uid=seat.seat_uid,
                section=seat.section,
                row_label=seat.row_label,
                seat_number=seat.seat_number,
                label=seat.label,
                x=seat.x,
                y=seat.y,
                category_key=seat.category_key,
                status=seat.status,
                accessibility=seat.accessibility,
            )
            for seat in to_create
        ]
    )
    try:
        session.flush()
    except IntegrityError as exc:
        # UNIQUE(performance_id, seat_uid). Unreachable sequentially - we just
        # subtracted the existing set - so this means a concurrent publish of the
        # same performance. Surface it as that, not as a raw driver error.
        raise ConcurrentPublish(
            f"performance {performance_id} is being published concurrently; "
            f"inventory was not materialised twice",
            performance_id=performance_id,
        ) from exc

    return len(to_create), len(parsed.seats) - len(to_create)


def publish_performance(
    session: Session,
    performance: Performance | int,
    *,
    prices: Mapping[str, Any] | None = None,
    currency: str = DEFAULT_CURRENCY,
    actor_user_id: int | None = None,
    activate: bool = True,
) -> PublishResult:
    """Freeze the layout version and materialise this performance's inventory.

    `prices` maps a layout category key to its price for THIS performance, in
    minor units: `{"vip": 25000, "standard": 12000}` is KWD 25.000 / 12.000.

    Safe to call repeatedly: the second call creates no seats, re-prices the
    categories if the prices changed, and leaves the already-frozen layout alone.
    """
    performance_id = _resolve_id(performance)

    with unit_of_work(session):
        perf, version, organization_id = load_publish_context(session, performance_id)
        moment = clock.now(session)

        parsed = parse_layout(version.layout_data)
        normalized_prices = normalize_prices(prices, currency)
        _validate_prices(parsed, normalized_prices)

        froze = _freeze(session, version, moment)
        categories_created, categories_updated = _sync_categories(
            session, perf.id, parsed, normalized_prices
        )
        seats_created, seats_existing = _materialize_seats(session, perf.id, parsed)

        if activate and perf.status == "draft":
            perf.status = "on_sale"
        session.flush()

        if froze:
            record_audit(
                session,
                organization_id=organization_id,
                action=ACTION_LAYOUT_FROZEN,
                entity_type="layout_version",
                entity_id=version.id,
                actor_user_id=actor_user_id,
                data={
                    "performance_id": perf.id,
                    "version_number": version.version_number,
                    "frozen_at": moment,
                    "seat_count": len(parsed.seats),
                },
            )

        record_audit(
            session,
            organization_id=organization_id,
            action=ACTION_PERFORMANCE_PUBLISHED,
            entity_type="performance",
            entity_id=perf.id,
            actor_user_id=actor_user_id,
            data={
                "layout_version_id": version.id,
                "froze_layout": froze,
                "seats_created": seats_created,
                "seats_existing": seats_existing,
                "categories_created": categories_created,
                "categories_updated": categories_updated,
                "status": perf.status,
            },
        )

        result = PublishResult(
            performance_id=perf.id,
            layout_version_id=version.id,
            froze_layout=froze,
            seats_created=seats_created,
            seats_existing=seats_existing,
            categories_created=categories_created,
            categories_updated=categories_updated,
            status=perf.status,
        )

    return result
