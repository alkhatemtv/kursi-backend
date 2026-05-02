"""Event CRUD routes.

Public:
  GET  /events                — list all active events (homepage)
  GET  /events/{id}           — view a single event

Organizer-only:
  GET  /events/mine           — list MY events (any status)
  POST /events                — create a new event (status defaults to 'draft')
  PUT  /events/{id}           — update one of MY events
  DELETE /events/{id}         — delete one of MY events
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_organizer
from app.database import get_db
from app.models import Booking, Event, User
from app.schemas import EventCreate, EventOut, EventUpdate, Message

router = APIRouter(prefix="/events", tags=["events"])


def _hydrate(event: Event, db: Session) -> EventOut:
    """Compute live capacity / sold / revenue for an event."""
    capacity = len(event.seats or [])
    confirmed = (
        db.query(Booking)
        .filter(Booking.event_id == event.id, Booking.status == "confirmed")
        .all()
    )
    sold = sum(len(b.seats or []) for b in confirmed)
    revenue = sum(b.total for b in confirmed)
    out = EventOut.model_validate(event)
    out.capacity = capacity
    out.sold_count = sold
    out.revenue = revenue
    return out


# ── Public ──────────────────────────────────────────────────
@router.get("", response_model=list[EventOut])
def list_public_events(db: Session = Depends(get_db)):
    """Anyone (logged in or not) can browse active events."""
    events = db.query(Event).filter(Event.status == "active").all()
    return [_hydrate(e, db) for e in events]


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _hydrate(event, db)


# ── Organizer-only ──────────────────────────────────────────
@router.get("/mine/list", response_model=list[EventOut])
def list_my_events(
    user: User = Depends(require_organizer), db: Session = Depends(get_db)
):
    """Returns ALL of the organizer's events regardless of status."""
    events = db.query(Event).filter(Event.organizer_id == user.id).all()
    return [_hydrate(e, db) for e in events]


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: EventCreate,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    event = Event(
        organizer_id=user.id,
        name=payload.name,
        description=payload.description,
        venue=payload.venue,
        event_date=payload.event_date,
        icon=payload.icon,
        tag=payload.tag,
        stage_w=payload.stage_w,
        stage_h=payload.stage_h,
        seats=[s.model_dump() for s in payload.seats],
        categories=[c.model_dump() for c in payload.categories],
        status=payload.status,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return _hydrate(event, db)


@router.put("/{event_id}", response_model=EventOut)
def update_event(
    event_id: int,
    payload: EventUpdate,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.organizer_id != user.id:
        raise HTTPException(
            status_code=403, detail="You don't own this event"
        )

    data = payload.model_dump(exclude_unset=True)
    # Convert nested pydantic objects to plain dicts for JSON storage
    if "seats" in data and data["seats"] is not None:
        data["seats"] = [s.model_dump() if hasattr(s, "model_dump") else s for s in data["seats"]]
    if "categories" in data and data["categories"] is not None:
        data["categories"] = [c.model_dump() if hasattr(c, "model_dump") else c for c in data["categories"]]

    for field, value in data.items():
        setattr(event, field, value)

    db.commit()
    db.refresh(event)
    return _hydrate(event, db)


@router.delete("/{event_id}", response_model=Message)
def delete_event(
    event_id: int,
    user: User = Depends(require_organizer),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.organizer_id != user.id:
        raise HTTPException(
            status_code=403, detail="You don't own this event"
        )
    db.delete(event)
    db.commit()
    return Message(detail=f"Event {event_id} deleted")
