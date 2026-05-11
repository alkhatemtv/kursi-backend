"""Pydantic schemas — request bodies and response shapes."""
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ── Users ───────────────────────────────────────────────────
class UserBase(BaseModel):
    email: EmailStr
    name: str | None = None
    role: str = "customer"
    org_name: str | None = None
    phone: str | None = None


class UserOut(UserBase):
    id: int
    auth0_sub: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class UserUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    org_name: str | None = None
    phone: str | None = None


# ── Events ──────────────────────────────────────────────────
class CategoryItem(BaseModel):
    id: str
    name: str
    price: float
    color: str = "#94a3b8"


class SeatItem(BaseModel):
    id: str | int
    x: float
    y: float
    catId: str
    row: int | None = None
    col: int | None = None
    label: str | None = None
    blocked: bool = False
    customPrice: float | None = None


class EventBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    venue: str | None = None
    event_date: str | None = None
    icon: str = "🎫"
    tag: str = "Event"
    stage_w: int = 1400
    stage_h: int = 900
    seats: list[SeatItem] = []
    categories: list[CategoryItem] = []
    status: str = "draft"
    performer: str | None = None
    gallery: list[str] = []
    duration_minutes: int | None = None


class EventCreate(EventBase):
    pass


class EventUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    venue: str | None = None
    event_date: str | None = None
    icon: str | None = None
    tag: str | None = None
    stage_w: int | None = None
    stage_h: int | None = None
    seats: list[SeatItem] | None = None
    categories: list[CategoryItem] | None = None
    status: str | None = None
    performer: str | None = None
    gallery: list[str] | None = None
    duration_minutes: int | None = None


class EventOut(EventBase):
    id: int
    organizer_id: int
    created_at: datetime
    updated_at: datetime
    # Computed at response time so the frontend doesn't have to recalc
    capacity: int = 0
    sold_count: int = 0
    revenue: float = 0.0
    view_count: int = 0
    min_price: float | None = None
    model_config = ConfigDict(from_attributes=True)


class PriceRange(BaseModel):
    min: float | None = None
    max: float | None = None


class VenueInfo(BaseModel):
    name: str | None = None


class EventSummary(BaseModel):
    """Compact event payload used for `related_events`."""
    id: int
    name: str
    venue: str | None = None
    event_date: str | None = None
    tag: str
    icon: str
    min_price: float | None = None
    model_config = ConfigDict(from_attributes=True)


class EventDetailOut(EventOut):
    """Extended event payload returned by GET /events/{id}."""
    venue_info: VenueInfo
    price_range: PriceRange
    related_events: list[EventSummary] = []


class EventListResponse(BaseModel):
    events: list[EventOut]
    total: int
    page: int
    per_page: int
    total_pages: int


class SortOption(str, Enum):
    date_asc = "date_asc"
    date_desc = "date_desc"
    price_asc = "price_asc"
    price_desc = "price_desc"
    popularity = "popularity"


# ── Bookings ────────────────────────────────────────────────
class BookingSeat(BaseModel):
    label: str
    category: str
    price: float


class BookingCreate(BaseModel):
    event_id: int
    seats: list[BookingSeat]
    customer_name: str | None = None
    customer_email: EmailStr | None = None
    payment_ref: str | None = None  # fake — frontend can pass a mock token


class BookingOut(BaseModel):
    id: int
    ref: str
    customer_id: int
    event_id: int
    seats: list[BookingSeat]
    total: float
    customer_name: str | None = None
    customer_email: str | None = None
    status: str
    payment_ref: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ── Refunds ─────────────────────────────────────────────────
class RefundCreate(BaseModel):
    booking_id: int
    reason: str | None = None


class RefundOut(BaseModel):
    id: int
    booking_id: int
    amount: float
    reason: str | None = None
    status: str
    created_at: datetime
    processed_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


# ── Wishlist ────────────────────────────────────────────────
class WishlistAdd(BaseModel):
    event_id: int


class WishlistItem(BaseModel):
    event_id: int
    added_at: datetime
    event: EventSummary
    model_config = ConfigDict(from_attributes=True)


# ── Generic ─────────────────────────────────────────────────
class Message(BaseModel):
    detail: str


class HealthResponse(BaseModel):
    status: str
    env: str
