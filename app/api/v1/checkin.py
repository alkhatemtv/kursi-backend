"""The door: resolving a scanned credential to a verdict (spec 5).

WHY EVERY VERDICT IS AN HTTP 200
--------------------------------
A turnstile is not asking "did my request succeed". It is asking "do I open".
`already_checked_in` and `cancelled` are perfectly successful answers to that
question, and encoding them as 4xx would mean every scanner app had to parse
error bodies to run its main loop - and would make a genuine network failure
indistinguishable from a genuine "this ticket is refunded". So the request
succeeds and `verdict` carries the answer. Only 401/403 (the SCANNER is not
allowed here) and 422 (the body is malformed) are error statuses.

WHY AN UNKNOWN TICKET AND ANOTHER ORG'S TICKET ARE THE SAME VERDICT
-------------------------------------------------------------------
Both are `invalid`. If a foreign ticket produced its own verdict, a scanner
could be walked over the credential space to learn which tokens are real
somewhere else. `invalid` means "not a ticket you can admit", and the reason is
deliberately not narrowed further.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import (
    SCAN,
    Credential,
    Principal,
    any_credential,
    bind_organization,
)
from app.api.keys import SCOPE_WRITE
from app.api.v1.schemas import CheckInOut, CheckInRequest
from app.database import get_db
from app.engine_models import PerformanceSeat, Ticket
from app.engine_services.clock import as_utc
from app.engine_services.credentials import credential_hash, verify_credential
from app.engine_services.errors import InvalidTicketTransition
from app.engine_services.fulfilment import check_in_ticket

router = APIRouter(tags=["check-in"])

_AUTH_LINE = (
    "user token (owner, admin, box_office, scanner) · API key with `write` scope"
)


def _verdict(
    verdict: str,
    message: str,
    ticket: Ticket | None = None,
    seat_uid: str | None = None,
    seat_label: str | None = None,
) -> CheckInOut:
    return CheckInOut(
        verdict=verdict,
        message=message,
        ticket_id=ticket.id if ticket else None,
        performance_id=ticket.performance_id if ticket else None,
        seat_uid=seat_uid,
        seat_label=seat_label,
        checked_in_at=as_utc(ticket.checked_in_at) if ticket else None,
    )


@router.post(
    "/checkin",
    response_model=CheckInOut,
    summary="Scan a ticket credential",
    description=(
        "Resolves a QR payload and, when the ticket is admissible, checks it in "
        "- both in one call, so two turnstiles cannot both admit the same "
        "ticket.\n\n"
        "**Always returns 200 with a `verdict`.** A refused ticket is a "
        "successful request; do not treat non-`valid` as an error.\n\n"
        "| verdict | meaning |\n"
        "|---|---|\n"
        "| `valid` | Admitted. The ticket is now `checked_in`; this is the only "
        "verdict that changes anything. |\n"
        "| `already_checked_in` | Someone already came in on it. `checked_in_at` "
        "says when. |\n"
        "| `cancelled` | The ticket was cancelled; its seat is back on sale. |\n"
        "| `refunded` | The ticket was refunded. |\n"
        "| `wrong_performance` | A real, live ticket - for a different show. |\n"
        "| `superseded` | A real ticket whose credential has since been rotated. "
        "This is an old QR: a screenshot, a forwarded email, a printout from "
        "before the reissue. |\n"
        "| `invalid` | Not a credential we issued, or not one you may admit. |\n\n"
        "**Send `performance_id`.** Without it, a valid ticket for tomorrow's "
        "show is admitted at tonight's door - the credential itself does not "
        "know which gate it was presented at.\n\n"
        "**Auth:** " + _AUTH_LINE
    ),
)
def check_in(
    body: CheckInRequest,
    credential: Credential = Depends(any_credential),
    db: Session = Depends(get_db),
) -> CheckInOut:
    resolved = verify_credential(body.credential)
    if resolved is None:
        # Not signed by us: forged, corrupted, or from another deployment.
        return _verdict("invalid", "this is not a credential issued by Kursi")

    ticket_id, version = resolved
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _verdict("invalid", "this credential does not name a ticket")

    # The caller must be allowed to work this ticket's organization. A `None`
    # here means "not your organization at all" and is reported as `invalid`;
    # a member with the wrong ROLE still gets a 403, because their problem is
    # permissions, not a bad ticket.
    principal: Principal | None = bind_organization(
        db, credential, ticket.organization_id, roles=SCAN, scope=SCOPE_WRITE
    )
    if principal is None:
        return _verdict("invalid", "this credential does not name a ticket")

    if ticket.credential_version != version:
        return _verdict(
            "superseded",
            "this credential has been replaced by a newer one for the same "
            "ticket; ask the holder for their current QR",
            ticket,
        )
    if ticket.credential_hash is not None and credential_hash(
        body.credential
    ) != ticket.credential_hash:
        # The version matched but the token did not. Signature verification
        # already passed, so this is the stored-hash cross-check catching a
        # credential that was minted with a different signing key.
        return _verdict("invalid", "this credential does not match the ticket")

    seat_uid = seat_label = None
    seat = db.get(PerformanceSeat, ticket.performance_seat_id)
    if seat is not None:
        seat_uid, seat_label = seat.seat_uid, seat.label

    if body.performance_id is not None and ticket.performance_id != body.performance_id:
        return _verdict(
            "wrong_performance",
            "this ticket is for a different performance",
            ticket,
            seat_uid,
            seat_label,
        )

    if ticket.status == "cancelled":
        return _verdict("cancelled", "this ticket was cancelled", ticket, seat_uid, seat_label)
    if ticket.status == "refunded":
        return _verdict("refunded", "this ticket was refunded", ticket, seat_uid, seat_label)
    if ticket.status == "checked_in":
        return _verdict(
            "already_checked_in",
            "this ticket has already been used",
            ticket,
            seat_uid,
            seat_label,
        )

    try:
        admitted = check_in_ticket(
            db, ticket.id, actor_user_id=principal.actor_user_id
        )
    except InvalidTicketTransition:
        # Lost the race to another turnstile between the read above and the
        # conditional UPDATE. The database decided; report what it decided.
        db.rollback()
        current = db.get(Ticket, ticket_id)
        return _verdict(
            "already_checked_in",
            "this ticket has already been used",
            current,
            seat_uid,
            seat_label,
        )

    return _verdict("valid", "admit", admitted, seat_uid, seat_label)
