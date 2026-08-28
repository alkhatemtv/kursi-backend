"""Tenant-scoped row lookups shared by the /v1 routers.

EVERY LOOKUP CARRIES THE ORGANISATION
-------------------------------------
None of these takes an id on its own. `get_venue(db, org_id, venue_id)` returns
the venue only if it belongs to that organisation, and raises `NotFound`
otherwise - so a caller who guesses another tenant's venue id gets exactly the
response they would get for an id that does not exist. Writing the join here
once is what stops a single forgotten `WHERE organization_id = ...` in a route
from becoming a cross-tenant read.

Layouts and layout versions have no `organization_id` column of their own; they
inherit tenancy through venue -> organisation, which is why those two are
joins rather than `session.get`.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine_models import (
    EngineEvent,
    LayoutVersion,
    Order,
    Performance,
    PerformanceSeat,
    Ticket,
    Venue,
    VenueLayout,
)
from app.engine_services.errors import NotFound


def get_venue(session: Session, organization_id: int, venue_id: int) -> Venue:
    venue = session.execute(
        select(Venue).where(Venue.id == venue_id, Venue.organization_id == organization_id)
    ).scalar_one_or_none()
    if venue is None:
        raise NotFound(f"venue {venue_id} does not exist")
    return venue


def get_layout(session: Session, organization_id: int, layout_id: int) -> VenueLayout:
    layout = session.execute(
        select(VenueLayout)
        .join(Venue, Venue.id == VenueLayout.venue_id)
        .where(VenueLayout.id == layout_id, Venue.organization_id == organization_id)
    ).scalar_one_or_none()
    if layout is None:
        raise NotFound(f"layout {layout_id} does not exist")
    return layout


def get_layout_version(
    session: Session, organization_id: int, version_id: int
) -> LayoutVersion:
    version = session.execute(
        select(LayoutVersion)
        .join(VenueLayout, VenueLayout.id == LayoutVersion.venue_layout_id)
        .join(Venue, Venue.id == VenueLayout.venue_id)
        .where(LayoutVersion.id == version_id, Venue.organization_id == organization_id)
    ).scalar_one_or_none()
    if version is None:
        raise NotFound(f"layout version {version_id} does not exist")
    return version


def get_event(session: Session, organization_id: int, event_id: int) -> EngineEvent:
    event = session.execute(
        select(EngineEvent).where(
            EngineEvent.id == event_id, EngineEvent.organization_id == organization_id
        )
    ).scalar_one_or_none()
    if event is None:
        raise NotFound(f"event {event_id} does not exist")
    return event


def get_performance(
    session: Session, organization_id: int, performance_id: int
) -> Performance:
    performance = session.execute(
        select(Performance)
        .join(EngineEvent, EngineEvent.id == Performance.event_id)
        .where(
            Performance.id == performance_id,
            EngineEvent.organization_id == organization_id,
        )
    ).scalar_one_or_none()
    if performance is None:
        raise NotFound(f"performance {performance_id} does not exist")
    return performance


def get_order(session: Session, organization_id: int, order_id: int) -> Order:
    order = session.execute(
        select(Order).where(
            Order.id == order_id, Order.organization_id == organization_id
        )
    ).scalar_one_or_none()
    if order is None:
        raise NotFound(f"order {order_id} does not exist")
    return order


def get_ticket(session: Session, organization_id: int, ticket_id: int) -> Ticket:
    ticket = session.execute(
        select(Ticket).where(
            Ticket.id == ticket_id, Ticket.organization_id == organization_id
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise NotFound(f"ticket {ticket_id} does not exist")
    return ticket


def ticket_out(session: Session, ticket: Ticket):
    """One ticket, with its seat's uid and label filled in."""
    from app.api.v1.schemas import TicketOut

    uid, label = seat_labels(session, [ticket.performance_seat_id]).get(
        ticket.performance_seat_id, (None, None)
    )
    return TicketOut.model_validate(ticket).model_copy(
        update={"seat_uid": uid, "seat_label": label}
    )


def ticket_page(session: Session, rows: list[Ticket]) -> list:
    """A page of tickets with their seats resolved in ONE extra query.

    Naming the seat is what makes a ticket list readable, and doing it per row
    would turn a 200-ticket page into 200 statements.
    """
    from app.api.v1.schemas import TicketOut

    labels = seat_labels(session, [t.performance_seat_id for t in rows])
    out = []
    for ticket in rows:
        uid, label = labels.get(ticket.performance_seat_id, (None, None))
        out.append(
            TicketOut.model_validate(ticket).model_copy(
                update={"seat_uid": uid, "seat_label": label}
            )
        )
    return out


def seat_labels(session: Session, seat_ids: list[int]) -> dict[int, tuple[str, str | None]]:
    """`{seat_id: (seat_uid, label)}` for a batch of inventory rows.

    Ticket responses name the seat, and a list of 200 tickets must not become
    200 seat queries; every caller resolves the whole page in one statement.
    """
    if not seat_ids:
        return {}
    rows = session.execute(
        select(PerformanceSeat.id, PerformanceSeat.seat_uid, PerformanceSeat.label).where(
            PerformanceSeat.id.in_(seat_ids)
        )
    ).all()
    return {row.id: (row.seat_uid, row.label) for row in rows}
