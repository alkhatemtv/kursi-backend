"""Order completion and the ticket lifecycle (spec 5 and 6).

COMPLETION IS A HAND-OFF, NOT AN ADDITION
-----------------------------------------
Before completion, the seat is held by an ACTIVE LOCK, which is temporary and
timestamp-judged. After completion it is held by a LIVE TICKET, which is
permanent until cancelled. `complete_order` performs that hand-off in one
transaction: issue the tickets, then release the locks. The locks are released
deliberately - leaving them would give a seat two owners and would keep dead
rows in the partial index forever. The ticket index
(`UNIQUE(performance_seat_id) WHERE status IN ('issued','checked_in')`) takes
over as the backstop the moment the lock lets go.

WHY THE EXPIRY CHECK IS INSIDE THE TRANSACTION
----------------------------------------------
"The order was live when the payment webhook arrived" is not good enough. The
transition is a conditional UPDATE whose WHERE clause carries the liveness test,
so the database decides whether the order was still alive at the instant of
writing. If it matched zero rows, someone else's hold has already taken the
seats and the completion must fail rather than double-sell them.

USAGE IS MONOTONIC
------------------
`usage_events` gets one row per issued ticket and is never touched again.
Cancelling or refunding a ticket frees the SEAT but does not remove the usage
row: it was issued, that consumed quota, and billing must not be reversible by
customer-service actions (Decision 4). The tests assert this explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.engine_models import (
    Order,
    PerformanceSeat,
    SeatLock,
    Ticket,
    UsageEvent,
)
from app.engine_services import clock
from app.engine_services.audit import (
    ACTION_ORDER_COMPLETED,
    ACTION_TICKET_CANCELLED,
    ACTION_TICKET_CHECKED_IN,
    ACTION_TICKET_CREDENTIAL_ROTATED,
    ACTION_TICKET_ISSUED,
    ACTION_TICKET_REFUNDED,
    record_audit,
)
from app.engine_services.availability import (
    LIVE_ORDER_STATUSES,
    LIVE_TICKET_STATUSES,
    describe_unavailable,
)
from app.engine_services.credentials import issue_credential
from app.engine_services.errors import (
    EngineConflict,
    InvalidTicketTransition,
    NotFound,
    OrderNotLive,
    SeatsUnavailable,
    ValidationError,
)
from app.engine_services.pricing import load_performance_categories, price_seats
from app.engine_services.uow import unit_of_work

#: Ticket transitions permitted in Phase 1b (spec 5 names the states but not the
#: edges). Terminal states accept nothing further; a refund of a checked-in
#: ticket is allowed because attendance does not preclude a refund decision.
TICKET_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "issued": ("checked_in", "cancelled", "refunded"),
    "checked_in": ("refunded",),
    "cancelled": (),
    "refunded": (),
}


@dataclass
class CompletionResult:
    """What completion produced. `credentials` is the ONLY time the tokens
    exist - the database stores their hashes, so they cannot be re-derived."""

    order_id: int
    ticket_ids: list[int] = field(default_factory=list)
    credentials: dict[int, str] = field(default_factory=dict)
    usage_event_ids: list[int] = field(default_factory=list)
    locks_released: int = 0
    total_minor: int = 0
    currency: str = "KWD"


def _resolve_id(value: Any) -> int:
    return value.id if hasattr(value, "id") else int(value)


def complete_order(
    session: Session,
    order: Order | int,
    *,
    actor_user_id: int | None = None,
) -> CompletionResult:
    """Turn a live order into issued tickets. All-or-nothing.

    Raises `OrderNotLive` if the hold expired or the order was already completed
    or cancelled, and `SeatsUnavailable` if a seat stopped being sellable while
    the order held it (an operator blocking it, say).
    """
    order_id = _resolve_id(order)

    with unit_of_work(session):
        current = session.get(Order, order_id)
        if current is None:
            raise NotFound(f"order {order_id} does not exist")

        moment = clock.now(session)
        organization_id = current.organization_id
        performance_id = current.performance_id
        session.expire(current)

        # The liveness test lives in the WHERE clause: the database decides
        # whether this order was still alive at the moment of writing.
        transitioned = session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status.in_(LIVE_ORDER_STATUSES),
                Order.expires_at.is_not(None),
                Order.expires_at > moment,
            )
            .values(status="completed", updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if (transitioned.rowcount or 0) != 1:
            refreshed = session.get(Order, order_id)
            expired = refreshed.status in LIVE_ORDER_STATUSES
            raise OrderNotLive(
                f"order {order_id} is not live: "
                + ("its hold expired" if expired else f"it is {refreshed.status}"),
                order_id=order_id,
                status=refreshed.status,
                expired=expired,
            )

        locks = (
            session.execute(
                select(SeatLock)
                .where(SeatLock.order_id == order_id, SeatLock.released_at.is_(None))
                .order_by(SeatLock.performance_seat_id)
            )
            .scalars()
            .all()
        )
        if not locks:
            raise ValidationError(
                f"order {order_id} holds no seats and cannot be completed",
                order_id=order_id,
            )

        seat_ids = [lock.performance_seat_id for lock in locks]

        # Our own locks are excluded; anything else blocking these seats is a
        # genuine reason not to issue.
        conflicts = describe_unavailable(
            session, performance_id, seat_ids, moment, exclude_order_id=order_id
        )
        if conflicts:
            raise SeatsUnavailable(
                conflicts,
                f"order {order_id} cannot be completed: "
                f"{len(conflicts)} seat(s) are no longer sellable",
            )

        seats = (
            session.execute(
                select(PerformanceSeat)
                .where(PerformanceSeat.id.in_(seat_ids))
                .order_by(PerformanceSeat.id)
            )
            .scalars()
            .all()
        )
        categories = load_performance_categories(session, performance_id)
        amounts, currency = price_seats(list(seats), categories)

        tickets = [
            Ticket(
                order_id=order_id,
                organization_id=organization_id,
                performance_id=performance_id,
                performance_seat_id=seat.id,
                status="issued",
                credential_version=1,
                issued_at=moment,
                amount_paid_minor=amounts[seat.id],
                currency=currency,
            )
            for seat in seats
        ]
        session.add_all(tickets)
        try:
            session.flush()
        except IntegrityError as exc:
            # The never-double-sell backstop. Unreachable while the lock
            # discipline above holds - which is exactly why it must surface as a
            # loud, named conflict rather than a driver error.
            raise EngineConflict(
                f"double-sell backstop fired while completing order {order_id}: "
                f"a live ticket already exists for one of these seats",
                order_id=order_id,
                seat_ids=seat_ids,
            ) from exc

        credentials: dict[int, str] = {}
        for ticket in tickets:
            credential = issue_credential(ticket.id, ticket.credential_version)
            ticket.credential_hash = credential.hash
            credentials[ticket.id] = credential.token

        usage = [
            UsageEvent(
                organization_id=organization_id,
                ticket_id=ticket.id,
                occurred_at=moment,
            )
            for ticket in tickets
        ]
        session.add_all(usage)
        session.flush()

        # The hand-off: tickets now hold these seats, so the locks let go.
        released = session.execute(
            update(SeatLock)
            .where(SeatLock.order_id == order_id, SeatLock.released_at.is_(None))
            .values(released_at=moment)
            .execution_options(synchronize_session=False)
        )

        total_minor = sum(amounts.values())
        for ticket in tickets:
            record_audit(
                session,
                organization_id=organization_id,
                action=ACTION_TICKET_ISSUED,
                entity_type="ticket",
                entity_id=ticket.id,
                actor_user_id=actor_user_id,
                data={
                    "order_id": order_id,
                    "performance_seat_id": ticket.performance_seat_id,
                    "credential_version": ticket.credential_version,
                    "amount_paid_minor": ticket.amount_paid_minor,
                    "currency": ticket.currency,
                },
            )
        record_audit(
            session,
            organization_id=organization_id,
            action=ACTION_ORDER_COMPLETED,
            entity_type="order",
            entity_id=order_id,
            actor_user_id=actor_user_id,
            data={
                "ticket_ids": [t.id for t in tickets],
                "seat_ids": seat_ids,
                "locks_released": released.rowcount or 0,
                "total_minor": total_minor,
                "currency": currency,
            },
        )

        result = CompletionResult(
            order_id=order_id,
            ticket_ids=[t.id for t in tickets],
            credentials=credentials,
            usage_event_ids=[u.id for u in usage],
            locks_released=released.rowcount or 0,
            total_minor=total_minor,
            currency=currency,
        )

    return result


# ── Ticket lifecycle ────────────────────────────────────────────────────────
def _transition_ticket(
    session: Session,
    ticket: Ticket | int,
    target: str,
    action: str,
    *,
    actor_user_id: int | None,
    reason: str | None,
    extra_values: dict[str, Any] | None = None,
    extra_audit: dict[str, Any] | None = None,
) -> Ticket:
    """The one ticket state machine. `extra_values` are columns that belong to a
    particular edge - `checked_in_at` on check-in, say - written in the SAME
    conditional UPDATE as the status, so they can never land on a row whose
    transition someone else won."""
    ticket_id = _resolve_id(ticket)

    with unit_of_work(session):
        current = session.get(Ticket, ticket_id)
        if current is None:
            raise NotFound(f"ticket {ticket_id} does not exist")

        allowed = TICKET_TRANSITIONS.get(current.status, ())
        if target not in allowed:
            raise InvalidTicketTransition(
                f"ticket {ticket_id} is {current.status}; it cannot become "
                f"{target}"
                + (f" (allowed: {', '.join(allowed)})" if allowed else " (terminal)"),
                ticket_id=ticket_id,
                status=current.status,
                target=target,
            )

        moment = clock.now(session)
        organization_id = current.organization_id
        previous_status = current.status
        seat_id = current.performance_seat_id
        session.expire(current)

        changed = session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == previous_status)
            .values(status=target, updated_at=func.now(), **(extra_values or {}))
            .execution_options(synchronize_session=False)
        )
        if (changed.rowcount or 0) != 1:  # pragma: no cover - concurrent change
            raise InvalidTicketTransition(
                f"ticket {ticket_id} changed status concurrently",
                ticket_id=ticket_id,
                target=target,
            )

        record_audit(
            session,
            organization_id=organization_id,
            action=action,
            entity_type="ticket",
            entity_id=ticket_id,
            actor_user_id=actor_user_id,
            data={
                "from": previous_status,
                "to": target,
                "performance_seat_id": seat_id,
                "reason": reason,
                "occurred_at": moment,
                # Said out loud because it is the surprising part: the seat goes
                # back on sale, the usage row does not go away.
                "usage_event_retained": True,
                **(extra_audit or {}),
            },
        )

    updated = session.get(Ticket, ticket_id)
    session.refresh(updated)
    return updated


def cancel_ticket(
    session: Session,
    ticket: Ticket | int,
    *,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> Ticket:
    """issued -> cancelled. The seat becomes sellable again; usage is untouched."""
    return _transition_ticket(
        session,
        ticket,
        "cancelled",
        ACTION_TICKET_CANCELLED,
        actor_user_id=actor_user_id,
        reason=reason,
    )


def refund_ticket(
    session: Session,
    ticket: Ticket | int,
    *,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> Ticket:
    """issued/checked_in -> refunded. The seat becomes sellable again; usage is
    untouched (Decision 4: monetary reversal is not usage reversal)."""
    return _transition_ticket(
        session,
        ticket,
        "refunded",
        ACTION_TICKET_REFUNDED,
        actor_user_id=actor_user_id,
        reason=reason,
    )


def check_in_ticket(
    session: Session,
    ticket: Ticket | int,
    *,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> Ticket:
    """issued -> checked_in, stamping who scanned it and when (spec 5).

    THE DOOR IS A RACE
    ------------------
    Two scanners on two turnstiles can present the same QR in the same
    millisecond, and exactly one of them must be told "valid". That is settled
    here by the same conditional UPDATE every other transition uses - the second
    scanner matches zero rows and gets `InvalidTicketTransition`, which the
    check-in endpoint reports as the `already_checked_in` verdict. No lock, no
    read-then-write window.

    The verdict TABLE itself (valid / superseded / wrong_performance / ...) is
    the API layer's job, as spec 5 says; this is only the state change it makes
    when the verdict is `valid`.
    """
    return _transition_ticket(
        session,
        ticket,
        "checked_in",
        ACTION_TICKET_CHECKED_IN,
        actor_user_id=actor_user_id,
        reason=reason,
        extra_values={
            "checked_in_at": clock.now(session),
            "checked_in_by_user_id": actor_user_id,
        },
    )


@dataclass
class RotationResult:
    """`token` is the ONLY time the new credential exists in readable form."""

    ticket_id: int
    credential_version: int
    token: str


def rotate_credential(
    session: Session,
    ticket: Ticket | int,
    *,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> RotationResult:
    """Reissue a ticket's QR without reissuing the ticket (Decision 4).

    `credential_version` increments, a fresh token is signed and only its hash is
    stored. `tickets.id`, `status`, the seat and the usage row are all untouched
    - a customer who lost their phone gets a working QR back, and nothing about
    the sale changes.

    Every previously issued token for this ticket now carries a version that no
    longer matches the row, which is exactly what makes `superseded` a
    distinguishable scan verdict rather than a silent failure.

    The version bump is a conditional UPDATE on the version we read, so two
    simultaneous rotations cannot both claim the same new version: the loser
    matches zero rows and is told to retry.
    """
    ticket_id = _resolve_id(ticket)

    with unit_of_work(session):
        current = session.get(Ticket, ticket_id)
        if current is None:
            raise NotFound(f"ticket {ticket_id} does not exist")
        if current.status not in LIVE_TICKET_STATUSES:
            raise InvalidTicketTransition(
                f"ticket {ticket_id} is {current.status}; a credential is only "
                f"rotated for a live ticket",
                ticket_id=ticket_id,
                status=current.status,
            )

        organization_id = current.organization_id
        previous_version = current.credential_version
        next_version = previous_version + 1
        credential = issue_credential(ticket_id, next_version)
        session.expire(current)

        changed = session.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.credential_version == previous_version,
            )
            .values(
                credential_version=next_version,
                credential_hash=credential.hash,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        if (changed.rowcount or 0) != 1:  # pragma: no cover - concurrent rotation
            raise EngineConflict(
                f"ticket {ticket_id} was rotated concurrently; retry",
                ticket_id=ticket_id,
            )

        record_audit(
            session,
            organization_id=organization_id,
            action=ACTION_TICKET_CREDENTIAL_ROTATED,
            entity_type="ticket",
            entity_id=ticket_id,
            actor_user_id=actor_user_id,
            data={
                "from_version": previous_version,
                "to_version": next_version,
                "reason": reason,
                # The token is never audited - only its version. An audit log
                # that carried working credentials would defeat hashing them.
            },
        )

        result = RotationResult(
            ticket_id=ticket_id,
            credential_version=next_version,
            token=credential.token,
        )

    return result


def order_tickets(session: Session, order: Order | int) -> list[Ticket]:
    order_id = _resolve_id(order)
    return list(
        session.execute(
            select(Ticket).where(Ticket.order_id == order_id).order_by(Ticket.id)
        ).scalars()
    )


def live_ticket_for_seat(session: Session, seat_id: int) -> Ticket | None:
    return session.execute(
        select(Ticket).where(
            Ticket.performance_seat_id == seat_id,
            Ticket.status.in_(LIVE_TICKET_STATUSES),
        )
    ).scalar_one_or_none()
