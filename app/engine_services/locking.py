"""The locking engine (spec 4, Decision 3) - the heart of Phase 1b.

    "session-grouped locks, 8+4 min, DB-timestamp truth"

THE ARBITER IS AN INDEX, NOT A FUNCTION
---------------------------------------
`uq_engine_seat_locks_active_seat` is UNIQUE on `(performance_seat_id) WHERE
released_at IS NULL`. Nothing in this module decides who wins a contested seat;
the INSERT does. The code path is deliberately

    INSERT the locks  ->  IntegrityError  ->  rollback  ->  diagnose  ->  raise

and never

    SELECT is it free?  ->  it is  ->  INSERT

because between those last two steps another request fits comfortably. That is
not a theoretical window: it is the entire failure mode this design exists to
close.

WHY THE VERIFICATION COMES *AFTER* THE INSERT
---------------------------------------------
The unique index only knows about locks. Two other things can make a seat
unsellable - a non-`available` inventory status, and a live ticket - and neither
is covered by it. Checking those BEFORE inserting would reintroduce exactly the
TOCTOU gap the index removes. Checking them AFTER is sound, because by then we
hold the lock: no competing order can be issuing a ticket for a seat we have
locked, and any ticket that was committed before us is visible to our read.
So the order is: take the lock, then look around. See `create_draft_order`.

EXPIRED HOLDS AND THE ONE PLACE A ROW MUST STILL MOVE
-----------------------------------------------------
Spec: "locks dead the microsecond expires_at passes ... expired order's seats
immediately lockable by another order". The availability predicate honours that
with no help - an expired order simply stops being live.

The *row*, though, still has `released_at IS NULL`, and the partial unique index
does not read `orders.expires_at`. So a literal "INSERT a second unreleased
lock" would fail against a dead one. The resolution is `_reclaim_dead_locks`:
before inserting, the same transaction releases locks belonging to orders that
are *provably* dead by the same predicate. This is not a sweeper in the
correctness path - it is inline, scoped to the seats being requested, runs in
the caller's own transaction, and is driven by the identical `now` the rest of
the call uses. `gc_expired_locks` is the same statement unscoped, run for
hygiene; nothing waits for it and nothing is incorrect if it never runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.engine_models import (
    ORDER_CHANNELS,
    EngineEvent,
    Order,
    Performance,
    SeatLock,
)
from app.engine_services import clock
from app.engine_services.audit import (
    ACTION_ORDER_CANCELLED,
    ACTION_ORDER_CREATED,
    ACTION_ORDER_EXPIRED,
    ACTION_ORDER_EXTENDED,
    record_audit,
)
from app.engine_services.availability import (
    LIVE_ORDER_STATUSES,
    describe_unavailable,
    order_live_expr,
    resolve_seat_uids,
)
from app.engine_services.errors import (
    REASON_LOCK_CONTENTION,
    EngineConflict,
    ExtensionAlreadyUsed,
    NotFound,
    OrderNotLive,
    SeatConflict,
    SeatsUnavailable,
    ValidationError,
)
from app.engine_services.pricing import load_performance_categories, price_seats
from app.engine_services.uow import unit_of_work

#: Decision 3: an eight-minute hold, extendable exactly once by four minutes.
DEFAULT_HOLD_MINUTES = 8
EXTENSION_MINUTES = 4

#: Performance states in which no inventory may be held at all. `paused` and
#: `sold_out` are deliberately NOT here: pausing is a sales-window policy and
#: box-office overrides are a Phase 1c concern, whereas these three mean the
#: performance has no sellable inventory in any channel.
NON_SELLING_PERFORMANCE_STATUSES = ("draft", "cancelled", "closed")


def _resolve_id(value: Any) -> int:
    return value.id if hasattr(value, "id") else int(value)


def _dedupe(seat_uids: list[str]) -> list[str]:
    """Preserve caller order, drop repeats. Asking for A-1 twice is one seat."""
    seen: set[str] = set()
    ordered: list[str] = []
    for uid in seat_uids:
        if uid not in seen:
            seen.add(uid)
            ordered.append(uid)
    return ordered


# ── Reclaiming dead locks ───────────────────────────────────────────────────
def _reclaim_dead_locks(
    session: Session, seat_ids: list[int], moment: datetime
) -> int:
    """Release unreleased locks held by orders that are no longer live.

    Set-based and idempotent: two racers running it concurrently release the
    same rows once. It only ever touches locks whose order fails
    `order_live_expr` at `moment`, so it can never steal a seat from a hold that
    is still valid - and an expired hold cannot come back to life, because
    `extend_order` refuses to extend a dead order.
    """
    if not seat_ids:
        return 0
    # Correlated EXISTS rather than `order_id IN (SELECT ...)`: the seat filter
    # picks the handful of candidate rows first, and liveness is then judged one
    # order at a time, instead of building a set over the whole orders table.
    order_is_dead = exists(
        select(1).where(Order.id == SeatLock.order_id, ~order_live_expr(Order, moment))
    )
    result = session.execute(
        update(SeatLock)
        .where(
            SeatLock.released_at.is_(None),
            SeatLock.performance_seat_id.in_(seat_ids),
            order_is_dead,
        )
        .values(released_at=moment)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


# ── Creating a hold ─────────────────────────────────────────────────────────
class _VerificationFailed(Exception):
    """Internal: unwinds the transaction carrying the conflicts to report."""

    def __init__(self, conflicts: list[SeatConflict]) -> None:
        super().__init__("post-lock verification failed")
        self.conflicts = conflicts


def _load_order_by_external_ref(
    session: Session, organization_id: int, external_ref: str
) -> Order | None:
    return session.execute(
        select(Order).where(
            Order.organization_id == organization_id,
            Order.external_ref == external_ref,
        )
    ).scalar_one_or_none()


def create_draft_order(
    session: Session,
    organization: Any,
    performance: Any,
    seat_uids: list[str],
    channel: str = "marketplace",
    external_ref: str | None = None,
    *,
    customer_name: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    actor_user_id: int | None = None,
    hold_minutes: int = DEFAULT_HOLD_MINUTES,
) -> Order:
    """Create a draft order holding every requested seat, or hold nothing.

    All-or-nothing: if any seat is unavailable the whole call raises
    `SeatsUnavailable`, listing one `SeatConflict` per offending seat with the
    reason (`locked`, `sold`, `seat_status`, `unknown_seat`). No partial hold is
    ever left behind.

    `external_ref` is the caller's idempotency key, unique per organization: the
    same key returns the order that key already created rather than locking a
    second set of seats.
    """
    organization_id = _resolve_id(organization)
    performance_id = _resolve_id(performance)

    if channel not in ORDER_CHANNELS:
        raise ValidationError(
            f"unknown channel {channel!r}; expected one of "
            f"{', '.join(ORDER_CHANNELS)}",
            channel=channel,
        )
    requested = _dedupe([str(uid) for uid in (seat_uids or [])])
    if not requested:
        raise ValidationError("no seats requested")
    if hold_minutes <= 0:
        raise ValidationError("hold_minutes must be positive", hold_minutes=hold_minutes)

    # Idempotency, cheap path: the key was used before, so no locking happens.
    if external_ref is not None:
        previous = _load_order_by_external_ref(session, organization_id, external_ref)
        if previous is not None:
            return previous

    moment = clock.now(session)
    expires_at = moment + timedelta(minutes=hold_minutes)

    try:
        with unit_of_work(session):
            perf = session.get(Performance, performance_id)
            if perf is None:
                raise NotFound(f"performance {performance_id} does not exist")

            event = session.get(EngineEvent, perf.event_id)
            if event is None or event.organization_id != organization_id:
                # Tenancy: an org may only sell its own inventory.
                raise NotFound(
                    f"performance {performance_id} does not belong to organization "
                    f"{organization_id}"
                )
            if perf.status in NON_SELLING_PERFORMANCE_STATUSES:
                raise EngineConflict(
                    f"performance {performance_id} is {perf.status} and is not "
                    f"selling",
                    performance_id=performance_id,
                    status=perf.status,
                )

            seats_by_uid, unknown = resolve_seat_uids(session, perf.id, requested)
            if unknown:
                raise SeatsUnavailable(unknown)

            seats = [seats_by_uid[uid] for uid in requested]
            seat_ids = sorted(seat.id for seat in seats)

            categories = load_performance_categories(session, perf.id)
            amounts, currency = price_seats(seats, categories)
            subtotal = sum(amounts.values())

            # Free up provably-dead holds on exactly these seats first, so an
            # expired order's rows cannot masquerade as a live claim.
            _reclaim_dead_locks(session, seat_ids, moment)

            order = Order(
                organization_id=organization_id,
                performance_id=perf.id,
                channel=channel,
                status="draft",
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                expires_at=expires_at,
                extended=False,
                subtotal_minor=subtotal,
                fees_minor=0,
                discount_minor=0,
                total_minor=subtotal,
                currency=currency,
                external_ref=external_ref,
            )
            session.add(order)
            session.flush()

            # THE ARBITER. Ascending seat id so two overlapping baskets can
            # never take each other's rows in opposite orders and deadlock.
            session.add_all(
                [
                    SeatLock(order_id=order.id, performance_seat_id=seat_id)
                    for seat_id in seat_ids
                ]
            )
            session.flush()

            # We hold the locks; now look around. Anything a competing order
            # committed before us (a ticket) or an operator changed (inventory
            # status) is visible here, and cannot change under us any more.
            conflicts = describe_unavailable(
                session, perf.id, seat_ids, moment, exclude_order_id=order.id
            )
            if conflicts:
                raise _VerificationFailed(conflicts)

            record_audit(
                session,
                organization_id=organization_id,
                action=ACTION_ORDER_CREATED,
                entity_type="order",
                entity_id=order.id,
                actor_user_id=actor_user_id,
                data={
                    "performance_id": perf.id,
                    "channel": channel,
                    "seat_uids": requested,
                    "seat_ids": seat_ids,
                    "expires_at": expires_at,
                    "hold_minutes": hold_minutes,
                    "total_minor": subtotal,
                    "currency": currency,
                    "external_ref": external_ref,
                },
            )
        return order

    except _VerificationFailed as failure:
        raise SeatsUnavailable(failure.conflicts) from None

    except IntegrityError as exc:
        # unit_of_work already rolled back. Two constraints can land here.
        session.rollback()

        if external_ref is not None:
            # Idempotency, racing path: two requests with one key. The loser
            # adopts the winner's order instead of failing.
            previous = _load_order_by_external_ref(
                session, organization_id, external_ref
            )
            if previous is not None:
                return previous

        # Otherwise it was the active-lock index. Diagnose in a FRESH
        # transaction so the report describes what is committed and true, and
        # close that transaction before raising - the caller is getting an
        # exception, not a session it is expected to tidy up.
        conflicts = _diagnose_lost_race(session, performance_id, requested, moment)
        session.rollback()
        raise SeatsUnavailable(conflicts) from exc


def _diagnose_lost_race(
    session: Session, performance_id: int, requested: list[str], moment: datetime
) -> list[SeatConflict]:
    """Explain a lost INSERT race, after the fact and from committed state."""
    seats_by_uid, unknown = resolve_seat_uids(session, performance_id, requested)
    seat_ids = [seat.id for seat in seats_by_uid.values()]
    conflicts = unknown + describe_unavailable(
        session, performance_id, seat_ids, moment
    )
    if conflicts:
        return conflicts

    # The winner's transaction rolled back between our error and this read, so
    # the seats look free again. Report contention rather than inventing a
    # holder we cannot name - the caller should simply retry.
    return [
        SeatConflict(
            seat_uid=uid,
            seat_id=seats_by_uid[uid].id if uid in seats_by_uid else None,
            reason=REASON_LOCK_CONTENTION,
            detail={"retryable": True},
        )
        for uid in requested
    ]


# ── Extending a hold ────────────────────────────────────────────────────────
def extend_order(
    session: Session,
    order: Order | int,
    *,
    actor_user_id: int | None = None,
    minutes: int = EXTENSION_MINUTES,
) -> Order:
    """The single +4:00 (Decision 3).

    One UPDATE of `orders.expires_at` covers every lock the order holds, because
    a lock has no deadline of its own - it inherits its order's. The UPDATE is a
    compare-and-swap on `(extended, status, expires_at)`, so two simultaneous
    extension requests cannot both succeed: the second matches zero rows and is
    rejected, not silently applied.
    """
    order_id = _resolve_id(order)

    with unit_of_work(session):
        current = session.get(Order, order_id)
        if current is None:
            raise NotFound(f"order {order_id} does not exist")

        moment = clock.now(session)
        previous_expiry = current.expires_at
        session.expire(current)

        result = session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.extended.is_(False),
                Order.status.in_(LIVE_ORDER_STATUSES),
                Order.expires_at.is_not(None),
                Order.expires_at > moment,
                Order.expires_at == previous_expiry,
            )
            .values(
                expires_at=previous_expiry + timedelta(minutes=minutes),
                extended=True,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )

        if (result.rowcount or 0) != 1:
            refreshed = session.get(Order, order_id)
            if refreshed.extended:
                raise ExtensionAlreadyUsed(
                    f"order {order_id} has already used its one "
                    f"{minutes}-minute extension",
                    order_id=order_id,
                    expires_at=(
                        refreshed.expires_at.isoformat()
                        if refreshed.expires_at
                        else None
                    ),
                )
            raise OrderNotLive(
                f"order {order_id} cannot be extended: it is {refreshed.status}"
                + ("" if refreshed.status not in LIVE_ORDER_STATUSES else " and expired"),
                order_id=order_id,
                status=refreshed.status,
                expired=refreshed.status in LIVE_ORDER_STATUSES,
            )

        refreshed = session.get(Order, order_id)
        record_audit(
            session,
            organization_id=refreshed.organization_id,
            action=ACTION_ORDER_EXTENDED,
            entity_type="order",
            entity_id=order_id,
            actor_user_id=actor_user_id,
            data={
                "minutes": minutes,
                "previous_expires_at": previous_expiry,
                "expires_at": refreshed.expires_at,
            },
        )

    session.refresh(refreshed)
    return refreshed


# ── Releasing a hold ────────────────────────────────────────────────────────
def release_order(
    session: Session,
    order: Order | int,
    *,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> Order:
    """Cancel an order and release its seats immediately.

    Cancelling an already-cancelled order is a no-op, and cancelling a hold that
    has already expired still works: the caller is tidying up something it no
    longer owns, which is harmless and should not be an error. Only a COMPLETED
    order refuses - those seats belong to tickets now, and giving them back is
    `cancel_ticket`'s job, not this one's.
    """
    order_id = _resolve_id(order)

    with unit_of_work(session):
        current = session.get(Order, order_id)
        if current is None:
            raise NotFound(f"order {order_id} does not exist")
        if current.status == "cancelled":
            return current
        if current.status not in LIVE_ORDER_STATUSES:
            raise OrderNotLive(
                f"order {order_id} is {current.status} and cannot be cancelled",
                order_id=order_id,
                status=current.status,
            )

        moment = clock.now(session)
        organization_id = current.organization_id
        session.expire(current)

        session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(LIVE_ORDER_STATUSES))
            .values(status="cancelled", updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        released = session.execute(
            update(SeatLock)
            .where(SeatLock.order_id == order_id, SeatLock.released_at.is_(None))
            .values(released_at=moment)
            .execution_options(synchronize_session=False)
        )

        record_audit(
            session,
            organization_id=organization_id,
            action=ACTION_ORDER_CANCELLED,
            entity_type="order",
            entity_id=order_id,
            actor_user_id=actor_user_id,
            data={"locks_released": released.rowcount or 0, "reason": reason},
        )

    cancelled = session.get(Order, order_id)
    session.refresh(cancelled)
    return cancelled


# ── Housekeeping (required by nothing) ──────────────────────────────────────
@dataclass
class GcResult:
    orders_expired: int
    locks_released: int


def gc_expired_locks(
    session: Session,
    *,
    organization_id: int | None = None,
    write_audit: bool = True,
) -> GcResult:
    """Tidy rows that timestamp truth has already made irrelevant.

    Moves `draft`/`awaiting_payment` orders past their deadline to `expired` and
    stamps `released_at` on every lock held by a non-live order.

    NOTHING DEPENDS ON THIS. Availability is decided by comparing timestamps, so
    the seats these rows refer to were sellable the instant the deadline passed,
    whether or not this ever runs. It exists to keep the tables readable, to
    make `released_at` mean what it says for analytics, and to bound the size of
    the partial index. Safe to run at any time, concurrently with live traffic,
    and idempotent.
    """
    with unit_of_work(session):
        moment = clock.now(session)

        expiring = select(Order.id, Order.organization_id).where(
            Order.status.in_(LIVE_ORDER_STATUSES), ~order_live_expr(Order, moment)
        )
        if organization_id is not None:
            expiring = expiring.where(Order.organization_id == organization_id)
        rows = session.execute(expiring).all()

        orders_expired = 0
        if rows:
            order_ids = [row.id for row in rows]
            result = session.execute(
                update(Order)
                .where(Order.id.in_(order_ids), Order.status.in_(LIVE_ORDER_STATUSES))
                .values(status="expired", updated_at=func.now())
                .execution_options(synchronize_session=False)
            )
            orders_expired = result.rowcount or 0

            if write_audit:
                for row in rows:
                    record_audit(
                        session,
                        organization_id=row.organization_id,
                        action=ACTION_ORDER_EXPIRED,
                        entity_type="order",
                        entity_id=row.id,
                        data={"expired_at": moment, "by": "gc_expired_locks"},
                    )

        dead_order = select(1).where(
            Order.id == SeatLock.order_id,
            Order.status.notin_(LIVE_ORDER_STATUSES),
        )
        if organization_id is not None:
            dead_order = dead_order.where(Order.organization_id == organization_id)
        released = session.execute(
            update(SeatLock)
            .where(SeatLock.released_at.is_(None), exists(dead_order))
            .values(released_at=moment)
            .execution_options(synchronize_session=False)
        )

        result_summary = GcResult(
            orders_expired=orders_expired, locks_released=released.rowcount or 0
        )

    return result_summary
