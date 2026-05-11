"""SQLAlchemy ORM models — the four core tables."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    """End users — both customers and organizers.

    `auth0_sub` is the unique Auth0 subject identifier (e.g. "auth0|abc123")
    and is the primary way we identify a user across requests.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    auth0_sub: Mapped[str] = mapped_column(String, unique=True, index=True)
    email: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, default="customer")  # customer | organizer
    org_name: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    events: Mapped[list["Event"]] = relationship(back_populates="organizer", cascade="all, delete-orphan")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    wishlist: Mapped[list["Wishlist"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Event(Base):
    """An event organizers create — holds seat layout + categories as JSON."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organizer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    event_date: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # ISO string
    icon: Mapped[str] = mapped_column(String, default="🎫")
    tag: Mapped[str] = mapped_column(String, default="Event", index=True)  # Theater | Cinema | Talk Show | Custom

    # Seat builder data
    stage_w: Mapped[int] = mapped_column(Integer, default=1400)
    stage_h: Mapped[int] = mapped_column(Integer, default=900)
    seats: Mapped[list] = mapped_column(JSON, default=list)        # [{id,x,y,catId,row,col,label,blocked}]
    categories: Mapped[list] = mapped_column(JSON, default=list)   # [{id,name,price,color}]

    # Detail-page metadata
    performer: Mapped[str | None] = mapped_column(String, nullable=True)
    gallery: Mapped[list] = mapped_column(JSON, default=list)      # ["https://...", ...]
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Denormalised from `categories` so we can index/sort/filter by price in SQL
    min_price: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0, index=True)

    status: Mapped[str] = mapped_column(String, default="draft")   # draft | active | inactive | scheduled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organizer: Mapped["User"] = relationship(back_populates="events")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class Booking(Base):
    """A customer purchase — stores selected seats and total."""

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    ref: Mapped[str] = mapped_column(String, unique=True, index=True)  # KURSI-XXXXXX
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)

    # Snapshot of seat selection at purchase time
    # [{label, category, price}]
    seats: Mapped[list] = mapped_column(JSON, default=list)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    customer_name: Mapped[str | None] = mapped_column(String, nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(String, default="confirmed")  # confirmed | refunded | cancelled
    payment_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # fake payment id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    customer: Mapped["User"] = relationship(back_populates="bookings")
    event: Mapped["Event"] = relationship(back_populates="bookings")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="booking", cascade="all, delete-orphan")


class Refund(Base):
    """A refund request initiated by an organizer."""

    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    booking: Mapped["Booking"] = relationship(back_populates="refunds")


class Wishlist(Base):
    """A user's saved-for-later events. Composite PK enforces one-row-per (user, event)."""

    __tablename__ = "wishlist"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), primary_key=True, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="wishlist")
    event: Mapped["Event"] = relationship()
