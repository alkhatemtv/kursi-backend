"""The availability predicate - defined ONCE, reused by everything.

    available  ==  inventory status is 'available'
               AND no ACTIVE lock  (unreleased, held by a LIVE order)
               AND no LIVE ticket  (issued or checked_in)

    live order ==  status IN (draft, awaiting_payment)
               AND expires_at IS NOT NULL AND expires_at > :now

Spec 3 is explicit that `sold` and `locked` are NOT seat statuses: sold is the
existence of a live ticket, locked is the existence of an active lock. That is
what keeps the three tables from drifting - but it only holds if exactly one
piece of code knows how to combine them. This module is that piece. The locking
engine, order completion and any future seat-map endpoint all call in here; none
of them re-implements "is it free".

EXPIRY IS A COMPARISON, NOT A JOB
---------------------------------
Nothing below consults a status column to learn that a hold died. A lock whose
order passed `expires_at` one microsecond ago simply stops satisfying
`order_live_expr`, in the same query, with no sweeper having run.
`gc_expired_locks` tidies the rows afterwards; it is never consulted here.

A live order MUST carry a deadline. `expires_at IS NULL` is treated as "not
live" everywhere (here, in the reclaim, and in the GC) rather than as "never
expires": an unbounded hold on a seat is the one outcome the engine must not
produce by accident.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session, aliased

from app.engine_models import Order, PerformanceSeat, SeatLock, Ticket
from app.engine_services.clock import as_utc
from app.engine_services.errors import (
    REASON_LOCKED,
    REASON_SEAT_STATUS,
    REASON_SOLD,
    REASON_UNKNOWN_SEAT,
    SeatConflict,
)

#: An order in one of these states may still be holding seats - if it also has a
#: deadline in the future.
LIVE_ORDER_STATUSES = ("draft", "awaiting_payment")
#: Ticket states that occupy a seat. cancelled/refunded do not (spec 5).
LIVE_TICKET_STATUSES = ("issued", "checked_in")
#: The only inventory status that may be sold.
SELLABLE_SEAT_STATUS = "available"


def order_live_expr(order_cls, now: datetime):
    """SQL: is this order still holding its seats at `now`?"""
    return and_(
        order_cls.status.in_(LIVE_ORDER_STATUSES),
        order_cls.expires_at.is_not(None),
        order_cls.expires_at > now,
    )


def _blocking_lock_columns(now: datetime, *, exclude_order_id: int | None):
    """Correlated scalar subqueries describing the ACTIVE lock on each seat.

    Returns (holder_order_id, holder_expires_at). Both are NULL exactly when no
    active lock exists - which is how the boolean predicate and the per-seat
    diagnosis stay literally the same expression.

    `exclude_order_id` lets an order ignore its OWN lock. Completion needs this:
    by then the order holds every seat, and without the exclusion it would
    diagnose itself as the thief.
    """
    lock = aliased(SeatLock)
    order = aliased(Order)
    base = (
        select(lock.order_id, order.expires_at)
        .join(order, lock.order_id == order.id)
        .where(
            lock.performance_seat_id == PerformanceSeat.id,
            lock.released_at.is_(None),
            order_live_expr(order, now),
        )
    )
    if exclude_order_id is not None:
        base = base.where(lock.order_id != exclude_order_id)
    base = base.correlate(PerformanceSeat).limit(1)
    holder = base.with_only_columns(lock.order_id).scalar_subquery()
    holder_expiry = base.with_only_columns(order.expires_at).scalar_subquery()
    return holder, holder_expiry


def _blocking_ticket_columns():
    """Correlated scalar subqueries describing the LIVE ticket on each seat."""
    ticket = aliased(Ticket)
    base = (
        select(ticket.id, ticket.status)
        .where(
            ticket.performance_seat_id == PerformanceSeat.id,
            ticket.status.in_(LIVE_TICKET_STATUSES),
        )
        .correlate(PerformanceSeat)
        .limit(1)
    )
    return (
        base.with_only_columns(ticket.id).scalar_subquery(),
        base.with_only_columns(ticket.status).scalar_subquery(),
    )


def seat_is_available_expr(now: datetime, *, exclude_order_id: int | None = None):
    """THE predicate, as a SQL boolean over `PerformanceSeat`."""
    holder, _ = _blocking_lock_columns(now, exclude_order_id=exclude_order_id)
    ticket_id, _ = _blocking_ticket_columns()
    return and_(
        PerformanceSeat.status == SELLABLE_SEAT_STATUS,
        holder.is_(None),
        ticket_id.is_(None),
    )


def available_seats_query(
    performance_id: int, now: datetime, *, exclude_order_id: int | None = None
) -> Select:
    """Every sellable seat of a performance, at `now`."""
    return (
        select(PerformanceSeat)
        .where(
            PerformanceSeat.performance_id == performance_id,
            seat_is_available_expr(now, exclude_order_id=exclude_order_id),
        )
        .order_by(PerformanceSeat.id)
    )


def available_seat_uids(
    session: Session,
    performance_id: int,
    now: datetime,
    *,
    exclude_order_id: int | None = None,
) -> list[str]:
    return list(
        session.execute(
            select(PerformanceSeat.seat_uid)
            .where(
                PerformanceSeat.performance_id == performance_id,
                seat_is_available_expr(now, exclude_order_id=exclude_order_id),
            )
            .order_by(PerformanceSeat.seat_uid)
        ).scalars()
    )


def is_seat_available(
    session: Session,
    seat_id: int,
    now: datetime,
    *,
    exclude_order_id: int | None = None,
) -> bool:
    found = session.execute(
        select(PerformanceSeat.id).where(
            PerformanceSeat.id == seat_id,
            seat_is_available_expr(now, exclude_order_id=exclude_order_id),
        )
    ).scalar_one_or_none()
    return found is not None


def resolve_seat_uids(
    session: Session, performance_id: int, seat_uids: list[str]
) -> tuple[dict[str, PerformanceSeat], list[SeatConflict]]:
    """Map requested seat_uids onto inventory rows.

    Unknown uids come back as conflicts rather than as a bare 404: a checkout
    sending a stale seat map should be told which seats it got wrong, in the
    same shape as every other seat failure.
    """
    rows = (
        session.execute(
            select(PerformanceSeat).where(
                PerformanceSeat.performance_id == performance_id,
                PerformanceSeat.seat_uid.in_(seat_uids),
            )
        )
        .scalars()
        .all()
    )
    found = {row.seat_uid: row for row in rows}
    missing = [
        SeatConflict(seat_uid=uid, reason=REASON_UNKNOWN_SEAT)
        for uid in seat_uids
        if uid not in found
    ]
    return found, missing


def describe_unavailable(
    session: Session,
    performance_id: int,
    seat_ids: list[int],
    now: datetime,
    *,
    exclude_order_id: int | None = None,
) -> list[SeatConflict]:
    """Per-seat diagnosis: which of `seat_ids` are NOT available, and why.

    Reasons are reported most-fundamental-first. A blocked seat that also
    carries a stale lock is reported as `seat_status`, because releasing the
    lock would not make it sellable.
    """
    if not seat_ids:
        return []

    holder, holder_expiry = _blocking_lock_columns(
        now, exclude_order_id=exclude_order_id
    )
    ticket_id, ticket_status = _blocking_ticket_columns()

    rows = session.execute(
        select(
            PerformanceSeat.id,
            PerformanceSeat.seat_uid,
            PerformanceSeat.status,
            holder.label("holder_order_id"),
            holder_expiry.label("holder_expires_at"),
            ticket_id.label("ticket_id"),
            ticket_status.label("ticket_status"),
        )
        .where(
            PerformanceSeat.performance_id == performance_id,
            PerformanceSeat.id.in_(seat_ids),
        )
        .order_by(PerformanceSeat.id)
    ).all()

    conflicts: list[SeatConflict] = []
    for row in rows:
        if row.status != SELLABLE_SEAT_STATUS:
            conflicts.append(
                SeatConflict(
                    seat_uid=row.seat_uid,
                    seat_id=row.id,
                    reason=REASON_SEAT_STATUS,
                    detail={"status": row.status},
                )
            )
        elif row.ticket_id is not None:
            conflicts.append(
                SeatConflict(
                    seat_uid=row.seat_uid,
                    seat_id=row.id,
                    reason=REASON_SOLD,
                    detail={
                        "ticket_id": row.ticket_id,
                        "ticket_status": row.ticket_status,
                    },
                )
            )
        elif row.holder_order_id is not None:
            expiry = as_utc(row.holder_expires_at)
            conflicts.append(
                SeatConflict(
                    seat_uid=row.seat_uid,
                    seat_id=row.id,
                    reason=REASON_LOCKED,
                    detail={
                        "held_by_order_id": row.holder_order_id,
                        "held_until": expiry.isoformat() if expiry else None,
                    },
                )
            )
    return conflicts
