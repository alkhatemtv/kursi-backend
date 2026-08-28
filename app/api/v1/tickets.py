"""The ticket lifecycle after the sale (spec 5)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Path
from sqlalchemy.orm import Session

from app.api.auth import (
    ANY_MEMBER,
    TICKET_ADMIN,
    TICKET_REVERSE,
    Access,
    Principal,
    org_from_ticket,
)
from app.api.keys import SCOPE_READ, SCOPE_WRITE
from app.api.v1.lookups import get_ticket, ticket_out
from app.api.v1.schemas import RotateCredentialOut, TicketActionRequest, TicketOut
from app.database import get_db
from app.engine_services.fulfilment import (
    cancel_ticket,
    refund_ticket,
    rotate_credential,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])

_READ = Access(org_from_ticket, roles=ANY_MEMBER, scope=SCOPE_READ)
_ROTATE = Access(org_from_ticket, roles=TICKET_ADMIN, scope=SCOPE_WRITE)
_REVERSE = Access(org_from_ticket, roles=TICKET_REVERSE, scope=SCOPE_WRITE)


@router.get(
    "/{ticket_id}",
    response_model=TicketOut,
    summary="Read a ticket",
    description="**Auth:** " + _READ.describe(),
)
def read_ticket(
    ticket_id: int = Path(...),
    principal: Principal = Depends(_READ),
    db: Session = Depends(get_db),
) -> TicketOut:
    return ticket_out(db, get_ticket(db, principal.organization_id, ticket_id))


@router.post(
    "/{ticket_id}/rotate-credential",
    response_model=RotateCredentialOut,
    summary="Reissue the QR without reissuing the ticket",
    description=(
        "Increments `credential_version`, signs a fresh token and stores only "
        "its hash. **Every credential issued for this ticket before now stops "
        "working** and scans as `superseded` rather than failing silently - "
        "which is the point: this is how a forwarded screenshot is invalidated.\n\n"
        "The ticket itself does not change. Same `id`, same seat, same status, "
        "same usage record, same row in every report. Only the QR is new.\n\n"
        "**The returned `credential` is shown once.** Only its hash is stored.\n\n"
        "Rotating a cancelled or refunded ticket is 409 - there is nothing live "
        "to carry a credential.\n\n"
        "**Auth:** " + _ROTATE.describe()
    ),
)
def rotate(
    ticket_id: int = Path(...),
    body: TicketActionRequest | None = Body(None),
    principal: Principal = Depends(_ROTATE),
    db: Session = Depends(get_db),
) -> RotateCredentialOut:
    ticket = get_ticket(db, principal.organization_id, ticket_id)
    result = rotate_credential(
        db,
        ticket.id,
        actor_user_id=principal.actor_user_id,
        reason=body.reason if body else None,
    )
    return RotateCredentialOut(
        ticket_id=result.ticket_id,
        credential_version=result.credential_version,
        credential=result.token,
    )


@router.post(
    "/{ticket_id}/cancel",
    response_model=TicketOut,
    summary="Cancel a ticket",
    description=(
        "The seat goes back on sale immediately.\n\n"
        "**The usage record is NOT reversed.** `engine_usage_events` keeps its "
        "row: the ticket was issued, that consumed quota, and billing must not "
        "be rewritable by a customer-service action. Cancelling is about the "
        "seat, not about the invoice.\n\n"
        "Only an `issued` ticket can be cancelled; a checked-in one has been "
        "used and can only be refunded. Terminal states are 409.\n\n"
        "**Auth:** " + _REVERSE.describe()
    ),
)
def cancel(
    ticket_id: int = Path(...),
    body: TicketActionRequest | None = Body(None),
    principal: Principal = Depends(_REVERSE),
    db: Session = Depends(get_db),
) -> TicketOut:
    ticket = get_ticket(db, principal.organization_id, ticket_id)
    updated = cancel_ticket(
        db,
        ticket.id,
        actor_user_id=principal.actor_user_id,
        reason=body.reason if body else None,
    )
    return ticket_out(db, updated)


@router.post(
    "/{ticket_id}/refund",
    response_model=TicketOut,
    summary="Refund a ticket",
    description=(
        "Marks the ticket refunded and frees its seat. Allowed from `issued` "
        "**and** from `checked_in`: someone who attended can still be refunded, "
        "and that decision is not ours to forbid.\n\n"
        "This records the decision; it does not move money. No payment provider "
        "is wired in yet.\n\n"
        "The usage record is retained, exactly as for a cancellation.\n\n"
        "**Auth:** " + _REVERSE.describe()
    ),
)
def refund(
    ticket_id: int = Path(...),
    body: TicketActionRequest | None = Body(None),
    principal: Principal = Depends(_REVERSE),
    db: Session = Depends(get_db),
) -> TicketOut:
    ticket = get_ticket(db, principal.organization_id, ticket_id)
    updated = refund_ticket(
        db,
        ticket.id,
        actor_user_id=principal.actor_user_id,
        reason=body.reason if body else None,
    )
    return ticket_out(db, updated)
