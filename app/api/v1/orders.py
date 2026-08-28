"""Checkout: the life of an order between holding seats and issuing tickets."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import ANY_MEMBER, SALES, Access, Principal, org_from_order
from app.api.keys import SCOPE_READ, SCOPE_WRITE
from app.api.pagination import Page, Paginated, page_params, paginate
from app.api.v1.lookups import get_order, ticket_page
from app.api.v1.schemas import (
    IssuedTicket,
    OrderCompleteOut,
    OrderDetail,
    OrderOut,
    TicketActionRequest,
    TicketOut,
)
from app.database import get_db
from app.engine_models import PerformanceSeat, SeatLock, Ticket
from app.engine_services.fulfilment import complete_order
from app.engine_services.locking import extend_order, release_order

router = APIRouter(prefix="/orders", tags=["checkout"])

_READ = Access(org_from_order, roles=ANY_MEMBER, scope=SCOPE_READ)
_WRITE = Access(org_from_order, roles=SALES, scope=SCOPE_WRITE)


def _order_seat_uids(db: Session, order_id: int) -> list[str]:
    """The seats this order took locks on, whether or not those are still held.

    Locks are not deleted when an order completes - `released_at` is stamped and
    the ticket takes over - so this stays the right answer for the whole life of
    the order, and it is one indexed join either way.
    """
    return list(
        db.execute(
            select(PerformanceSeat.seat_uid)
            .join(SeatLock, SeatLock.performance_seat_id == PerformanceSeat.id)
            .where(SeatLock.order_id == order_id)
            .order_by(PerformanceSeat.seat_uid)
        ).scalars()
    )


def _detail(db: Session, order) -> OrderDetail:
    return OrderDetail(
        **OrderOut.model_validate(order).model_dump(),
        seat_uids=_order_seat_uids(db, order.id),
    )


@router.get(
    "/{order_id}",
    response_model=OrderDetail,
    summary="Read an order",
    description=(
        "`status` is the state machine: `draft` → `awaiting_payment` → "
        "`completed`, or `expired`/`cancelled` from either live state. "
        "`expires_at` is meaningful only while the order is live.\n\n"
        "**Auth:** " + _READ.describe()
    ),
)
def read_order(
    order_id: int = Path(...),
    principal: Principal = Depends(_READ),
    db: Session = Depends(get_db),
) -> OrderDetail:
    return _detail(db, get_order(db, principal.organization_id, order_id))


@router.post(
    "/{order_id}/extend",
    response_model=OrderDetail,
    summary="Extend the hold, once",
    description=(
        "Adds four minutes to `expires_at`, covering every seat the order holds "
        "- a lock has no deadline of its own, it inherits the order's.\n\n"
        "**Exactly one extension is permitted per order.** A second attempt is "
        "409 `extension_already_used`. Extending an order whose hold has already "
        "lapsed is 409 `order_not_live` with `expired: true`: an expired hold "
        "cannot be brought back, because by then someone else may already own "
        "the seats.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def extend(
    order_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> OrderDetail:
    order = get_order(db, principal.organization_id, order_id)
    extended = extend_order(db, order.id, actor_user_id=principal.actor_user_id)
    return _detail(db, extended)


@router.post(
    "/{order_id}/release",
    response_model=OrderDetail,
    summary="Cancel the order and free its seats now",
    description=(
        "Puts the seats back on sale immediately rather than waiting for the "
        "hold to lapse. Call it when the customer abandons a basket.\n\n"
        "Releasing an order that is already cancelled, or whose hold already "
        "expired, succeeds and changes nothing - a client tidying up after "
        "itself should not have to reason about which of those happened. A "
        "**completed** order is refused with 409: those seats belong to tickets "
        "now, and giving them back is `POST /v1/tickets/{id}/cancel`.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def release(
    order_id: int = Path(...),
    body: TicketActionRequest | None = Body(None),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> OrderDetail:
    order = get_order(db, principal.organization_id, order_id)
    released = release_order(
        db,
        order.id,
        actor_user_id=principal.actor_user_id,
        reason=body.reason if body else None,
    )
    return _detail(db, released)


@router.post(
    "/{order_id}/complete",
    response_model=OrderCompleteOut,
    summary="Complete the order and issue tickets",
    description=(
        "The hand-off: the seats stop being held by a temporary lock and start "
        "being held by a permanent ticket, in one transaction.\n\n"
        "**Payment is not part of this call.** Phase 1c has no payment provider "
        "wired in, so completion is *trusted*: whoever calls this is asserting "
        "that the money is settled. Do not expose it to an untrusted client. A "
        "payment integration lands in a later phase and will move this behind a "
        "provider confirmation.\n\n"
        "The order must still be live at the instant of writing - the liveness "
        "test is inside the UPDATE, so \"it was live when my webhook fired\" is "
        "not good enough. A lapsed hold is 409 `order_not_live`; a seat that "
        "stopped being sellable while the order held it is 409 "
        "`seats_unavailable`. Either way no ticket is issued and no partial "
        "state is left behind.\n\n"
        "**The `credential` on each ticket is shown here and nowhere else.** "
        "Only its hash is stored. Deliver it to the customer now; a lost one is "
        "replaced with `POST /v1/tickets/{id}/rotate-credential`, never "
        "recovered.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def complete(
    order_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> OrderCompleteOut:
    order = get_order(db, principal.organization_id, order_id)
    result = complete_order(db, order.id, actor_user_id=principal.actor_user_id)

    tickets = (
        db.execute(
            select(Ticket.id, PerformanceSeat.seat_uid)
            .join(PerformanceSeat, PerformanceSeat.id == Ticket.performance_seat_id)
            .where(Ticket.id.in_(result.ticket_ids))
            .order_by(Ticket.id)
        )
        .all()
    )
    return OrderCompleteOut(
        order_id=result.order_id,
        total_minor=result.total_minor,
        currency=result.currency,
        tickets=[
            IssuedTicket(
                ticket_id=ticket_id,
                seat_uid=seat_uid,
                credential=result.credentials[ticket_id],
            )
            for ticket_id, seat_uid in tickets
        ],
    )


@router.get(
    "/{order_id}/tickets",
    response_model=Paginated[TicketOut],
    tags=["tickets"],
    summary="List an order's tickets",
    description=(
        "Credentials are NOT included - they are not stored in readable form. "
        "`credential_version` tells you how many times a ticket's QR has been "
        "reissued.\n\n"
        "**Auth:** " + _READ.describe()
    ),
)
def list_order_tickets(
    order_id: int = Path(...),
    principal: Principal = Depends(_READ),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[TicketOut]:
    order = get_order(db, principal.organization_id, order_id)
    statement = select(Ticket).where(Ticket.order_id == order.id).order_by(Ticket.id)
    rows, total = paginate(db, statement, page, count_over=Ticket.id)
    return Paginated[TicketOut](
        items=ticket_page(db, rows),
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
