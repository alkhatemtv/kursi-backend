"""Booking routes.

Customer:
  POST   /bookings           — purchase tickets (FAKE payment, no Stripe)
  GET    /bookings/mine      — list MY bookings

Organizer:
  GET    /bookings/event/{event_id}  — list bookings for one of MY events
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_organizer
from app.database import get_db
from app.models import Booking, Event, User
from app.schemas import BookingCreate, BookingOut

router = APIRouter(prefix="/bookings", tags=["bookings"])


def _generate_ref() -> str:
    """Generate a Kursi-style booking reference (e.g. KURSI-A1B2C3)."""
    return "KURSI-" + secrets.token_hex(3).upper()


def _validate_seat_availability(event: Event, requested_labels: list[str]) -> list[str]:
    """Returns a list of conflicting labels (already booked or blocked).

    Uses the JSON seat layout to check `blocked` flags, and the bookings table
    to check labels that have been booked by other confirmed bookings.
    """
    layout_seats = event.seats or []
    label_to_seat = {s.get("label"): s for s in layout_seats if s.get("label")}

    conflicts: list[str] = []
    for label in requested_labels:
        seat = label_to_seat.get(label)
        if seat and seat.get("blocked"):
            conflicts.append(label)

    # Find seats already taken by other confirmed bookings
    confirmed = [b for b in event.bookings if b.status == "confirmed"]
    booked_labels = set()
    for b in confirmed:
        for s in b.seats or []:
            if isinstance(s, dict) and s.get("label"):
                booked_labels.add(s["label"])

    for label in requested_labels:
        if label in booked_labels and label not in conflicts:
            conflicts.append(label)

    return conflicts


@router.post("", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
def create_booking(
    payload: BookingCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a confirmed booking. Payment is faked — `payment_ref` is whatever
    the frontend sends (usually a mock string). No Stripe integration."""
    event = db.query(Event).filter(Event.id == payload.event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.status != "active":
        raise HTTPException(status_code=400, detail="Event is not currently bookable")

    if not payload.seats:
        raise HTTPException(status_code=400, detail="At least one seat is required")

    requested_labels = [s.label for s in payload.seats]

    # No duplicates within this same booking
    if len(set(requested_labels)) != len(requested_labels):
        raise HTTPException(status_code=400, detail="Duplicate seats in request")

    conflicts = _validate_seat_availability(event, requested_labels)
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail=f"Seats no longer available: {', '.join(conflicts)}",
        )

    total = sum(s.price for s in payload.seats)

    booking = Booking(
        ref=_generate_ref(),
        customer_id=user.id,
        event_id=event.id,
        seats=[s.model_dump() for s in payload.seats],
        total=total,
        customer_name=payload.customer_name or user.name,
        customer_email=payload.customer_email or user.email,
        status="confirmed",
        payment_ref=payload.payment_ref or f"FAKE-{secrets.token_hex(4)}",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return booking


@router.get("/mine", response_model=list[BookingOut])
def list_my_bookings(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return (
        db.query(Booking)
        .filter(Booking.customer_id == user.id)
        .order_by(Booking.created_at.desc())
        .all()
    )


@router.get("/event/{event_id}", response_model=list[BookingOut])
def list_event_bookings(
    event_id: int,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    """Organizers can see all bookings for events they own."""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.organizer_id != user.id:
        raise HTTPException(status_code=403, detail="You don't own this event")
    return (
        db.query(Booking)
        .filter(Booking.event_id == event_id)
        .order_by(Booking.created_at.desc())
        .all()
    )


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A booking is visible to the customer who made it OR the event organizer."""
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.customer_id == user.id:
        return booking
    event = db.query(Event).filter(Event.id == booking.event_id).first()
    if event and event.organizer_id == user.id:
        return booking
    raise HTTPException(status_code=403, detail="Not authorized to view this booking")
