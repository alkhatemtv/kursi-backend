"""One performance: its inventory, its seat map, and starting a checkout on it."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import (
    ANY_MEMBER,
    EVENT_WRITE,
    SALES,
    Access,
    Principal,
    org_from_performance,
)
from app.api.keys import SCOPE_READ, SCOPE_WRITE
from app.api.pagination import Page, Paginated, page_params, paginate
from app.api.v1.lookups import get_performance, ticket_page
from app.api.v1.schemas import (
    AvailabilityCategory,
    AvailabilityCounts,
    AvailabilityOut,
    AvailabilitySeat,
    OrderCreate,
    OrderOut,
    PerformanceOut,
    PerformancePatch,
    PublishOut,
    PublishRequest,
    TicketOut,
)
from app.database import get_db
from app.engine_models import EngineEvent, Performance, Ticket
from app.engine_services import clock
from app.engine_services.audit import record_audit
from app.engine_services.availability import (
    PUBLIC_HELD,
    public_seat_status,
    seat_availability_rows,
)
from app.engine_services.clock import as_utc
from app.engine_services.locking import create_draft_order
from app.engine_services.pricing import load_performance_categories
from app.engine_services.publishing import publish_performance
from app.engine_services.uow import unit_of_work

router = APIRouter(prefix="/performances", tags=["performances"])

#: Performance and event states a stranger may see a seat map for. Anything
#: else - a draft still being built, a cancelled show, an archived event - is
#: visible only to the organization that owns it.
PUBLICLY_VISIBLE_PERFORMANCE = ("on_sale", "paused", "sold_out")
PUBLICLY_VISIBLE_EVENT = ("active", "coming_soon", "scheduled")


def _publicly_visible(
    session: Session, organization_id: int, params: dict[str, Any]
) -> bool:
    """May an unauthenticated caller see this performance's seat map?

    Availability is genuinely public information for a show that is on sale -
    the marketplace renders it to anonymous browsers - so requiring a credential
    would only mean every storefront shipped one. It is NOT public for a
    performance that is still a draft, or for a cancelled show: those are an
    organization's private plans.
    """
    performance = session.get(Performance, int(params["performance_id"]))
    if performance is None or performance.status not in PUBLICLY_VISIBLE_PERFORMANCE:
        return False
    event = session.get(EngineEvent, performance.event_id)
    return event is not None and event.status in PUBLICLY_VISIBLE_EVENT


_READ = Access(org_from_performance, roles=ANY_MEMBER, scope=SCOPE_READ)
_WRITE = Access(org_from_performance, roles=EVENT_WRITE, scope=SCOPE_WRITE)
_SELL = Access(org_from_performance, roles=SALES, scope=SCOPE_WRITE)
_MAP = Access(
    org_from_performance,
    roles=ANY_MEMBER,
    scope=SCOPE_READ,
    anonymous=_publicly_visible,
)


@router.get(
    "/{performance_id}",
    response_model=PerformanceOut,
    summary="Read a performance",
    description="**Auth:** " + _READ.describe(),
)
def read_performance(
    performance_id: int = Path(...),
    principal: Principal = Depends(_READ),
    db: Session = Depends(get_db),
) -> PerformanceOut:
    return PerformanceOut.model_validate(
        get_performance(db, principal.organization_id, performance_id)
    )


@router.patch(
    "/{performance_id}",
    response_model=PerformanceOut,
    summary="Update a performance",
    description=(
        "Times and lifecycle status. `layout_version_id` is deliberately not "
        "editable: a performance's seating is fixed for its whole life, which is "
        "what lets a ticket sold months ago still resolve to the seat it names. "
        "A different arrangement means a different performance.\n\n"
        "Setting `status` to `paused` stops new holds without releasing the ones "
        "already out; `cancelled` and `closed` stop selling outright.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def update_performance(
    body: PerformancePatch,
    performance_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> PerformanceOut:
    performance = get_performance(db, principal.organization_id, performance_id)
    changes = body.model_dump(exclude_unset=True)
    with unit_of_work(db):
        for field, value in changes.items():
            setattr(performance, field, value)
        if changes:
            record_audit(
                db,
                organization_id=principal.organization_id,
                action="performance.updated",
                entity_type="performance",
                entity_id=performance.id,
                actor_user_id=principal.actor_user_id,
                actor_api_key_id=principal.actor_api_key_id,
                data={"fields": sorted(changes)},
            )
    db.refresh(performance)
    return PerformanceOut.model_validate(performance)


@router.post(
    "/{performance_id}/publish",
    response_model=PublishOut,
    summary="Publish: freeze the layout, materialise inventory, set prices",
    description=(
        "The one call that turns a planned performance into something sellable. "
        "In a single transaction it:\n\n"
        "1. **freezes** the layout version, if this is the first performance to "
        "use it - after which its document can never be edited again;\n"
        "2. **materialises** one `performance_seats` row per seat in that "
        "document;\n"
        "3. **prices** every category the seats reference;\n"
        "4. moves the performance to `on_sale` (unless `activate` is false).\n\n"
        "Either all four happen or none do. A layout that would produce broken "
        "inventory is rejected with 422 *before* anything is frozen, listing "
        "every problem at once.\n\n"
        "**Prices are integer minor units.** `{\"vip\": 25000}` with currency "
        "KWD is KWD 25.000. A float is a 422. Every category the layout's seats "
        "reference must be priced and no others may be.\n\n"
        "**Safe to call again.** Re-publishing creates no duplicate seats "
        "(`seats_created` comes back 0), leaves the already-frozen layout alone, "
        "and *does* apply changed prices - repricing a performance is a normal "
        "operator action.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def publish(
    body: PublishRequest,
    performance_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> PublishOut:
    performance = get_performance(db, principal.organization_id, performance_id)
    result = publish_performance(
        db,
        performance.id,
        prices=body.prices,
        currency=body.currency.upper(),
        actor_user_id=principal.actor_user_id,
        activate=body.activate,
    )
    return PublishOut(
        performance_id=result.performance_id,
        layout_version_id=result.layout_version_id,
        froze_layout=result.froze_layout,
        seats_created=result.seats_created,
        seats_existing=result.seats_existing,
        seats_total=result.seats_total,
        categories_created=result.categories_created,
        categories_updated=result.categories_updated,
        status=result.status,
    )


@router.get(
    "/{performance_id}/availability",
    response_model=AvailabilityOut,
    tags=["availability"],
    summary="The seat map",
    description=(
        "Every seat of the performance with its current status, plus the price "
        "of every category. This is what a seat-map renderer draws and what a "
        "checkout reads before asking for seats.\n\n"
        "`status` is one of:\n\n"
        "- `available` — buyable right now.\n"
        "- `held` — inside another order's live hold. `held_until` says when "
        "that lapses; the seat becomes available again at that instant with no "
        "job needing to run.\n"
        "- `sold` — an issued or checked-in ticket holds it.\n"
        "- `blocked` — the inventory row itself is not sellable. "
        "`inventory_status` distinguishes `blocked` from `invitation` and "
        "`reserved_internal`.\n\n"
        "**`as_of` matters.** Holds are decided by comparing timestamps, so this "
        "map is true as of that instant and not afterwards. Treat it as a "
        "render, never as a reservation: the only thing that actually secures a "
        "seat is `POST /v1/performances/{id}/orders`, and it is allowed to "
        "refuse a seat this response called available.\n\n"
        "Computed in a single query pass however large the house is.\n\n"
        "**Auth:** " + _MAP.describe()
    ),
)
def availability(
    performance_id: int = Path(...),
    principal: Principal = Depends(_MAP),
    db: Session = Depends(get_db),
) -> AvailabilityOut:
    performance = get_performance(db, principal.organization_id, performance_id)
    moment = clock.now(db)

    categories = load_performance_categories(db, performance.id)
    rows = seat_availability_rows(db, performance.id, moment)

    seats: list[AvailabilitySeat] = []
    counts = {"available": 0, "held": 0, "sold": 0, "blocked": 0}
    for row in rows:
        public_status = public_seat_status(row)
        counts[public_status] += 1

        # Override beats category, exactly as `pricing.price_for_seat` decides
        # it. An unpriced seat reports null rather than 0: zero is a real price
        # and must not be invented.
        category = categories.get(row.category_key) if row.category_key else None
        if row.price_override_minor is not None:
            amount_minor = row.price_override_minor
            currency = row.currency or (category.currency if category else None)
        elif category is not None:
            amount_minor, currency = category.amount_minor, category.currency
        else:
            amount_minor, currency = None, None

        seats.append(
            AvailabilitySeat(
                uid=row.seat_uid,
                id=row.id,
                label=row.label,
                section=row.section,
                row=row.row_label,
                number=row.seat_number,
                x=row.x,
                y=row.y,
                category=row.category_key,
                status=public_status,
                inventory_status=row.status,
                accessibility=bool(row.accessibility),
                amount_minor=amount_minor,
                currency=currency,
                held_until=(
                    as_utc(row.holder_expires_at)
                    if public_status == PUBLIC_HELD
                    else None
                ),
            )
        )

    return AvailabilityOut(
        performance_id=performance.id,
        status=performance.status,
        starts_at=performance.starts_at,
        as_of=moment,
        counts=AvailabilityCounts(total=len(seats), **counts),
        categories=[
            AvailabilityCategory(
                key=c.category_key,
                name=c.name,
                name_ar=c.name_ar,
                color=c.color,
                amount_minor=c.amount_minor,
                currency=c.currency,
            )
            for c in sorted(categories.values(), key=lambda c: c.category_key)
        ],
        seats=seats,
    )


@router.post(
    "/{performance_id}/orders",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    tags=["checkout"],
    summary="Hold seats: create a draft order",
    description=(
        "Takes an 8-minute hold on every seat you name, or takes none at all.\n\n"
        "**All-or-nothing.** If any seat is unavailable the response is 409 "
        "`seats_unavailable` carrying a `conflicts` array with one entry per "
        "offending seat and a `reason` for each — `locked`, `sold`, "
        "`seat_status`, `unknown_seat`, or `lock_contention` (you lost a race; "
        "retry). Nothing is held and no order exists. Repaint exactly those "
        "seats.\n\n"
        "**The hold is durable the moment this returns.** A unique index in the "
        "database, not this process, decides who wins a contested seat, so two "
        "simultaneous requests for A-12 produce exactly one hold and one 409.\n\n"
        "`expires_at` is the deadline for **all** of the order's seats — there "
        "is no per-seat clock — and it is judged by comparing timestamps, so "
        "the seats free themselves the microsecond it passes. Extend it once "
        "with `POST /v1/orders/{id}/extend`.\n\n"
        "**Send an `external_ref`** if you can. It is your idempotency key, "
        "unique within your organization: retrying a request that may or may "
        "not have landed returns the original order instead of holding a second "
        "set of seats.\n\n"
        "**Auth:** " + _SELL.describe()
    ),
)
def create_order(
    body: OrderCreate,
    performance_id: int = Path(...),
    principal: Principal = Depends(_SELL),
    db: Session = Depends(get_db),
) -> OrderOut:
    performance = get_performance(db, principal.organization_id, performance_id)
    order = create_draft_order(
        db,
        principal.organization_id,
        performance.id,
        seat_uids=list(body.seat_uids),
        channel=body.channel,
        external_ref=body.external_ref,
        customer_name=body.customer_name,
        customer_email=body.customer_email,
        customer_phone=body.customer_phone,
        actor_user_id=principal.actor_user_id,
    )
    return OrderOut.model_validate(order)


@router.get(
    "/{performance_id}/tickets",
    response_model=Paginated[TicketOut],
    tags=["tickets"],
    summary="List a performance's tickets",
    description=(
        "Every ticket ever issued for the performance, including cancelled and "
        "refunded ones - those no longer hold their seat but they are still "
        "part of the sales record.\n\n"
        "**Auth:** " + _READ.describe()
    ),
)
def list_performance_tickets(
    performance_id: int = Path(...),
    principal: Principal = Depends(_READ),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[TicketOut]:
    performance = get_performance(db, principal.organization_id, performance_id)
    statement = (
        select(Ticket)
        .where(Ticket.performance_id == performance.id)
        .order_by(Ticket.id)
    )
    rows, total = paginate(db, statement, page, count_over=Ticket.id)
    return Paginated[TicketOut](
        items=ticket_page(db, rows),
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
