"""Event CRUD routes.

Public:
  GET  /events                — list active events with search/filter/sort/paging
  GET  /events/{id}           — view a single event (increments view_count)

Organizer-only:
  GET  /events/mine/list      — list MY events (any status)
  POST /events                — create a new event (status defaults to 'draft')
  PUT  /events/{id}           — update one of MY events
  DELETE /events/{id}         — delete one of MY events
"""
from datetime import date, datetime
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_organizer
from app.database import get_db
from app.models import Booking, Event, User
from app.schemas import (
    EventCreate,
    EventDetailOut,
    EventListResponse,
    EventOut,
    EventSummary,
    EventUpdate,
    Message,
    PriceRange,
    SortOption,
    VenueInfo,
)

router = APIRouter(prefix="/events", tags=["events"])


def _compute_min_price(categories: list | None) -> float | None:
    prices = [
        c.get("price")
        for c in (categories or [])
        if isinstance(c, dict) and c.get("price") is not None
    ]
    return float(min(prices)) if prices else None


def _compute_max_price(categories: list | None) -> float | None:
    prices = [
        c.get("price")
        for c in (categories or [])
        if isinstance(c, dict) and c.get("price") is not None
    ]
    return float(max(prices)) if prices else None


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


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _parse_iso_date(value: str, field: str) -> str:
    """Validate YYYY-MM-DD; return the original string for SQL string-comparison
    against the `event_date` column (which is stored as ISO strings)."""
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Invalid {field} — expected YYYY-MM-DD"},
        )
    return value


# ── Public ──────────────────────────────────────────────────
@router.get("")
def list_public_events(
    db: Session = Depends(get_db),
    search: str | None = Query(None),
    category: str | None = Query(None, description="One or more comma-separated categories"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    price_min: float | None = Query(None),
    price_max: float | None = Query(None),
    sort: str = Query("date_asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1),
):
    """Browse active events. All filters are AND-combined.

    Returns paginated `{events, total, page, per_page, total_pages}`.
    Calling with no query params still works — defaults to first page of 20,
    sorted by date ascending.
    """
    # Manual validation so we can return the {error: "..."} shape instead of
    # FastAPI's default 422/{"detail": [...]}.
    if per_page > 50:
        return _error(400, "per_page must be 50 or less")

    try:
        sort_opt = SortOption(sort)
    except ValueError:
        valid = ", ".join(s.value for s in SortOption)
        return _error(400, f"Invalid sort — must be one of: {valid}")

    if date_from is not None:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            return _error(400, "Invalid date_from — expected YYYY-MM-DD")
    if date_to is not None:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            return _error(400, "Invalid date_to — expected YYYY-MM-DD")

    q = db.query(Event).filter(Event.status == "active")

    if search:
        like = f"%{search.lower()}%"
        # Performer is a regular column; "venue name" is the venue string itself.
        q = q.filter(
            or_(
                Event.name.ilike(like),
                Event.venue.ilike(like),
                Event.performer.ilike(like),
            )
        )

    if category:
        cats = [c.strip() for c in category.split(",") if c.strip()]
        if cats:
            q = q.filter(Event.tag.in_(cats))

    if date_from:
        q = q.filter(Event.event_date >= date_from)
    if date_to:
        # Inclusive upper bound — anything starting that day still matches.
        q = q.filter(Event.event_date <= date_to + "T23:59:59")

    if price_min is not None:
        q = q.filter(Event.min_price >= price_min)
    if price_max is not None:
        q = q.filter(Event.min_price <= price_max)

    if sort_opt == SortOption.date_asc:
        q = q.order_by(Event.event_date.asc().nullslast(), Event.id.asc())
    elif sort_opt == SortOption.date_desc:
        q = q.order_by(Event.event_date.desc().nullslast(), Event.id.desc())
    elif sort_opt == SortOption.price_asc:
        q = q.order_by(Event.min_price.asc().nullslast(), Event.id.asc())
    elif sort_opt == SortOption.price_desc:
        q = q.order_by(Event.min_price.desc().nullslast(), Event.id.desc())
    elif sort_opt == SortOption.popularity:
        q = q.order_by(Event.view_count.desc(), Event.id.desc())

    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()

    return EventListResponse(
        events=[_hydrate(e, db) for e in rows],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=ceil(total / per_page) if total else 0,
    ).model_dump()


@router.get("/{event_id}", response_model=EventDetailOut)
def get_event(event_id: int, response: Response, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Bump the popularity counter. Single UPDATE — no concurrency guarantees.
    event.view_count = (event.view_count or 0) + 1
    db.commit()
    db.refresh(event)

    base = _hydrate(event, db)

    related_rows = (
        db.query(Event)
        .filter(
            Event.tag == event.tag,
            Event.id != event.id,
            Event.status == "active",
        )
        .order_by(Event.view_count.desc(), Event.id.desc())
        .limit(3)
        .all()
    )
    related = [EventSummary.model_validate(r) for r in related_rows]

    detail = EventDetailOut(
        **base.model_dump(),
        venue_info=VenueInfo(name=event.venue),
        price_range=PriceRange(
            min=_compute_min_price(event.categories),
            max=_compute_max_price(event.categories),
        ),
        related_events=related,
    )

    response.headers["Cache-Control"] = "public, max-age=300"
    return detail


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
    categories = [c.model_dump() for c in payload.categories]
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
        categories=categories,
        status=payload.status,
        performer=payload.performer,
        gallery=payload.gallery,
        duration_minutes=payload.duration_minutes,
        min_price=_compute_min_price(categories),
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
        # Keep denormalised min_price in sync with category prices.
        data["min_price"] = _compute_min_price(data["categories"])

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
