"""Refund routes (organizer-initiated).

  POST /refunds              — request a refund for a booking on MY event
  POST /refunds/{id}/approve — mark refund approved + cancel the booking
  GET  /refunds/mine         — list refunds across all my events
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import require_organizer
from app.database import get_db
from app.models import Booking, Event, Refund, User
from app.schemas import Message, RefundCreate, RefundOut

router = APIRouter(prefix="/refunds", tags=["refunds"])


@router.post("", response_model=RefundOut, status_code=status.HTTP_201_CREATED)
def create_refund(
    payload: RefundCreate,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    """Organizer initiates a refund. Booking must belong to one of their events
    and must currently be `confirmed`."""
    booking = db.query(Booking).filter(Booking.id == payload.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    event = db.query(Event).filter(Event.id == booking.event_id).first()
    if not event or event.organizer_id != user.id:
        raise HTTPException(status_code=403, detail="You don't own this event")

    if booking.status != "confirmed":
        raise HTTPException(
            status_code=400, detail=f"Cannot refund a booking with status '{booking.status}'"
        )

    # Block duplicate pending refunds for the same booking
    existing = (
        db.query(Refund)
        .filter(Refund.booking_id == booking.id, Refund.status == "pending")
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409, detail="A pending refund already exists for this booking"
        )

    refund = Refund(
        booking_id=booking.id,
        amount=booking.total,
        reason=payload.reason,
        status="pending",
    )
    db.add(refund)
    db.commit()
    db.refresh(refund)
    return refund


@router.post("/{refund_id}/approve", response_model=RefundOut)
def approve_refund(
    refund_id: int,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    """Mark the refund approved AND cancel the underlying booking, freeing the seats."""
    refund = db.query(Refund).filter(Refund.id == refund_id).first()
    if not refund:
        raise HTTPException(status_code=404, detail="Refund not found")

    booking = db.query(Booking).filter(Booking.id == refund.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Underlying booking not found")
    event = db.query(Event).filter(Event.id == booking.event_id).first()
    if not event or event.organizer_id != user.id:
        raise HTTPException(status_code=403, detail="You don't own this event")

    if refund.status != "pending":
        raise HTTPException(
            status_code=400, detail=f"Refund already {refund.status}"
        )

    refund.status = "approved"
    refund.processed_at = datetime.utcnow()
    booking.status = "refunded"
    db.commit()
    db.refresh(refund)
    return refund


@router.post("/{refund_id}/reject", response_model=RefundOut)
def reject_refund(
    refund_id: int,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    refund = db.query(Refund).filter(Refund.id == refund_id).first()
    if not refund:
        raise HTTPException(status_code=404, detail="Refund not found")

    booking = db.query(Booking).filter(Booking.id == refund.booking_id).first()
    event = db.query(Event).filter(Event.id == booking.event_id).first() if booking else None
    if not event or event.organizer_id != user.id:
        raise HTTPException(status_code=403, detail="You don't own this event")

    if refund.status != "pending":
        raise HTTPException(status_code=400, detail=f"Refund already {refund.status}")

    refund.status = "rejected"
    refund.processed_at = datetime.utcnow()
    db.commit()
    db.refresh(refund)
    return refund


@router.get("/mine", response_model=list[RefundOut])
def list_my_refunds(
    user: User = Depends(require_organizer), db: Session = Depends(get_db)
):
    """All refunds against any event I own."""
    my_event_ids = [
        e.id for e in db.query(Event).filter(Event.organizer_id == user.id).all()
    ]
    if not my_event_ids:
        return []

    booking_ids = [
        b.id
        for b in db.query(Booking).filter(Booking.event_id.in_(my_event_ids)).all()
    ]
    if not booking_ids:
        return []

    return (
        db.query(Refund)
        .filter(Refund.booking_id.in_(booking_ids))
        .order_by(Refund.created_at.desc())
        .all()
    )
