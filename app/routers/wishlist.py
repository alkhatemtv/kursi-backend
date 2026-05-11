"""Wishlist routes — saved-for-later events for the authenticated user.

  POST   /wishlist               — add an event   (body: {event_id})
  DELETE /wishlist/{event_id}    — remove an event
  GET    /wishlist               — list my saved events
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Event, User, Wishlist
from app.schemas import EventSummary, Message, WishlistAdd, WishlistItem

router = APIRouter(prefix="/wishlist", tags=["wishlist"])


@router.get("", response_model=list[WishlistItem])
def list_wishlist(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Wishlist)
        .filter(Wishlist.user_id == user.id)
        .order_by(Wishlist.added_at.desc())
        .all()
    )
    return [
        WishlistItem(
            event_id=row.event_id,
            added_at=row.added_at,
            event=EventSummary.model_validate(row.event),
        )
        for row in rows
        if row.event is not None
    ]


@router.post("", response_model=WishlistItem, status_code=status.HTTP_201_CREATED)
def add_to_wishlist(
    payload: WishlistAdd,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == payload.event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    existing = (
        db.query(Wishlist)
        .filter(Wishlist.user_id == user.id, Wishlist.event_id == event.id)
        .first()
    )
    if existing is None:
        existing = Wishlist(user_id=user.id, event_id=event.id)
        db.add(existing)
        db.commit()
        db.refresh(existing)

    return WishlistItem(
        event_id=existing.event_id,
        added_at=existing.added_at,
        event=EventSummary.model_validate(event),
    )


@router.delete("/{event_id}", response_model=Message)
def remove_from_wishlist(
    event_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(Wishlist)
        .filter(Wishlist.user_id == user.id, Wishlist.event_id == event_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Not in wishlist")
    db.delete(row)
    db.commit()
    return Message(detail=f"Event {event_id} removed from wishlist")
