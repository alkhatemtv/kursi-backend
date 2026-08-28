"""Request and response shapes for /v1.

CONVENTIONS THIS FILE ENFORCES (they are also the public API's conventions)
--------------------------------------------------------------------------
**Money.** Every monetary field is `*_minor`: an INTEGER count of the currency's
smallest unit, next to a 3-letter ISO `currency`. KWD has three minor digits, so
KWD 5.500 is `5500`; USD 12.99 is `1299`. There is no float anywhere in this
file and none is accepted on the wire - a decimal amount is a 422, not a
rounded number. This is Decision 5, and it is the single most important thing a
client integrating against this API has to get right.

**Time.** Every timestamp is UTC ISO-8601 with an explicit offset. SQLite hands
back naive datetimes for `timestamptz` columns while PostgreSQL hands back aware
ones, so every datetime crossing this boundary goes through `as_utc` - see
`UtcDatetime` below. A client must never have to guess a zone.

**Identity.** Ids are integers and stable. `seat_uid` is the stable string id a
seat carries from the layout document into inventory and is what a client uses
to ask for seats; `performance_seats.id` is an internal number that also appears
in conflict payloads for debugging.

**Optionality on PATCH.** Every PATCH body field defaults to `None` meaning
"leave it alone". There is deliberately no way to distinguish that from "set it
to null" for nullable fields; nothing in Phase 1c needs to null a field out, and
inventing a sentinel for it would be a contract we would have to keep.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from app.engine_services.clock import as_utc

#: Attach UTC to whatever the database handed back, and render it with an
#: explicit `+00:00` rather than pydantic's default `Z`.
#:
#: The offset form is not cosmetic. `SeatConflict.detail["held_until"]` is built
#: by the service layer with `datetime.isoformat()`, which writes `+00:00`, and a
#: client comparing a conflict's `held_until` against an order's `expires_at`
#: must find two strings it can compare - not one of each spelling. Serialising
#: only in JSON mode leaves `model_dump()` returning real datetimes, which the
#: few places that re-feed one model into another rely on.
UtcDatetime = Annotated[
    datetime,
    BeforeValidator(as_utc),
    PlainSerializer(
        lambda value: as_utc(value).isoformat(), return_type=str, when_used="json"
    ),
]


class Orm(BaseModel):
    """Base for anything read straight off an ORM row."""

    model_config = ConfigDict(from_attributes=True)


# ── /v1/me ──────────────────────────────────────────────────────────────────
class MembershipOut(BaseModel):
    organization_id: int
    organization_name: str
    organization_slug: str
    organization_type: str
    organization_plan: str
    role: str = Field(description="This user's role in that organization (spec 1).")
    status: str


class MeOut(BaseModel):
    id: int
    email: str
    name: str | None = None
    memberships: list[MembershipOut] = Field(
        description=(
            "Every organization this user belongs to. A user with no other "
            "membership always has at least one: a personal organization is "
            "provisioned on first authenticated request."
        )
    )


# ── Organizations ───────────────────────────────────────────────────────────
class OrganizationOut(Orm):
    id: int
    name: str
    name_ar: str | None = None
    slug: str
    type: str
    plan: str
    status: str
    branding: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    created_at: UtcDatetime


class OrganizationPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    name_ar: str | None = Field(None, max_length=200)
    branding: dict[str, Any] | None = Field(
        None,
        description=(
            "Free-form branding document (logo URL, colours, email sender). "
            "Phase 1c stores and returns it verbatim; the dashboard defines its "
            "shape."
        ),
    )


# ── Venues ──────────────────────────────────────────────────────────────────
class VenueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    name_ar: str | None = Field(None, max_length=200)
    address: str | None = None
    address_ar: str | None = None
    timezone: str = Field(
        "Asia/Kuwait",
        description="IANA zone the venue's local times are expressed in.",
    )


class VenuePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    name_ar: str | None = Field(None, max_length=200)
    address: str | None = None
    address_ar: str | None = None
    timezone: str | None = None


class VenueOut(Orm):
    id: int
    organization_id: int
    name: str
    name_ar: str | None = None
    address: str | None = None
    address_ar: str | None = None
    timezone: str
    created_at: UtcDatetime


# ── Layouts and layout versions ─────────────────────────────────────────────
class LayoutCreate(BaseModel):
    name: str = Field(
        min_length=1, max_length=200, examples=["Main Hall — Full"]
    )
    description: str | None = None


class LayoutOut(Orm):
    id: int
    venue_id: int
    name: str
    description: str | None = None
    created_at: UtcDatetime


class LayoutVersionCreate(BaseModel):
    layout_data: dict[str, Any] | None = Field(
        None,
        description=(
            "The authoring document: `{seats: [...], objects: [...], "
            "categories: [...]}`. Coordinates are canvas units at 50 px/metre. "
            "Omit it to copy the layout's most recent version, which is how you "
            "edit a frozen version: the copy comes back as a new draft."
        ),
    )


class LayoutDataUpdate(BaseModel):
    layout_data: dict[str, Any] = Field(
        description="Replaces the draft's document wholesale. Frozen versions reject this."
    )


class LayoutVersionOut(Orm):
    id: int
    venue_layout_id: int
    version_number: int
    status: Literal["draft", "frozen"]
    frozen_at: UtcDatetime | None = None
    created_by_user_id: int
    created_at: UtcDatetime


class LayoutVersionDetail(LayoutVersionOut):
    layout_data: dict[str, Any]


# ── Events ──────────────────────────────────────────────────────────────────
class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    title_ar: str | None = None
    description: str | None = None
    description_ar: str | None = None
    venue_id: int | None = None
    artwork_url: str | None = None
    cover_url: str | None = None
    category: str | None = None
    cast: dict[str, Any] | None = None
    policies: dict[str, Any] | None = Field(
        None, description="Terms, refund policy, age restriction, instructions."
    )
    status: Literal[
        "draft", "active", "coming_soon", "scheduled", "cancelled", "archived"
    ] = "draft"


class EventPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    title_ar: str | None = None
    description: str | None = None
    description_ar: str | None = None
    venue_id: int | None = None
    artwork_url: str | None = None
    cover_url: str | None = None
    category: str | None = None
    cast: dict[str, Any] | None = None
    policies: dict[str, Any] | None = None
    status: Literal[
        "draft", "active", "coming_soon", "scheduled", "cancelled", "archived"
    ] | None = None


class EventOut(Orm):
    id: int
    organization_id: int
    venue_id: int | None = None
    title: str
    title_ar: str | None = None
    description: str | None = None
    description_ar: str | None = None
    artwork_url: str | None = None
    cover_url: str | None = None
    category: str | None = None
    cast: dict[str, Any] = {}
    policies: dict[str, Any] = {}
    status: str
    created_at: UtcDatetime


# ── Performances ────────────────────────────────────────────────────────────
class PerformanceCreate(BaseModel):
    layout_version_id: int = Field(
        description=(
            "The seating source of truth for this performance. The version is "
            "frozen the first time the performance is published, and the "
            "performance keeps reading that frozen document for its whole life."
        )
    )
    starts_at: datetime
    doors_open_at: datetime | None = None
    duration_minutes: int | None = Field(None, ge=0)
    sales_open_at: datetime | None = None
    sales_close_at: datetime | None = None
    box_office_close_at: datetime | None = None


class PerformancePatch(BaseModel):
    starts_at: datetime | None = None
    doors_open_at: datetime | None = None
    duration_minutes: int | None = Field(None, ge=0)
    sales_open_at: datetime | None = None
    sales_close_at: datetime | None = None
    box_office_close_at: datetime | None = None
    status: Literal[
        "draft", "on_sale", "paused", "sold_out", "closed", "cancelled"
    ] | None = None


class PerformanceOut(Orm):
    id: int
    event_id: int
    layout_version_id: int
    starts_at: UtcDatetime
    doors_open_at: UtcDatetime | None = None
    duration_minutes: int | None = None
    sales_open_at: UtcDatetime | None = None
    sales_close_at: UtcDatetime | None = None
    box_office_close_at: UtcDatetime | None = None
    status: str
    created_at: UtcDatetime


class PublishRequest(BaseModel):
    prices: dict[str, Any] = Field(
        description=(
            "Price per layout category key, in INTEGER minor units. Either "
            "`{\"vip\": 25000}` or `{\"vip\": {\"amount_minor\": 25000, "
            "\"currency\": \"KWD\"}}`. `25000` with currency KWD is KWD 25.000. "
            "A float or a decimal string is rejected with 422 - money is never "
            "a float in this API. Every category the layout's seats actually "
            "reference must be priced, and pricing a key the layout does not "
            "define is an error too."
        ),
        examples=[{"vip": 25000, "standard": 12000}],
    )
    currency: str = Field(
        "KWD", min_length=3, max_length=3, description="ISO-4217 code, applied to any price given as a bare integer."
    )
    activate: bool = Field(
        True,
        description="Move a draft performance to `on_sale`. Set false to materialise inventory without opening sales.",
    )


class PublishOut(BaseModel):
    performance_id: int
    layout_version_id: int
    froze_layout: bool = Field(
        description="True when THIS call sealed the layout version. Publishing again returns false."
    )
    seats_created: int
    seats_existing: int
    seats_total: int
    categories_created: int
    categories_updated: int
    status: str


# ── Availability ────────────────────────────────────────────────────────────
class AvailabilitySeat(BaseModel):
    uid: str = Field(description="Stable seat id; this is what you send to create an order.")
    id: int = Field(description="Internal inventory row id, echoed in conflict payloads.")
    label: str | None = None
    section: str | None = None
    row: str | None = None
    number: str | None = None
    x: float | None = Field(None, description="Canvas units, 50 px/metre.")
    y: float | None = None
    category: str | None = Field(None, description="Category key; look it up in `categories`.")
    status: Literal["available", "held", "sold", "blocked"] = Field(
        description=(
            "`available` - buyable right now. `held` - inside another order's "
            "live hold; it may free itself when that hold expires. `sold` - an "
            "issued or checked-in ticket holds it. `blocked` - the inventory "
            "itself is not sellable (blocked, invitation-only or reserved)."
        )
    )
    inventory_status: str = Field(
        description="The raw `performance_seats.status`, for the cases where `blocked` is too coarse."
    )
    accessibility: bool = False
    amount_minor: int | None = Field(
        None, description="This seat's price, override first, else its category's."
    )
    currency: str | None = None
    held_until: UtcDatetime | None = Field(
        None, description="When the current hold lapses. Present only for `held`."
    )


class AvailabilityCategory(BaseModel):
    key: str
    name: str
    name_ar: str | None = None
    color: str | None = None
    amount_minor: int
    currency: str


class AvailabilityCounts(BaseModel):
    total: int
    available: int
    held: int
    sold: int
    blocked: int


class AvailabilityOut(BaseModel):
    performance_id: int
    status: str
    starts_at: UtcDatetime
    as_of: UtcDatetime = Field(
        description=(
            "The database instant this map was computed at. Holds are judged by "
            "timestamp comparison, so a map is only true as of this moment."
        )
    )
    counts: AvailabilityCounts
    categories: list[AvailabilityCategory]
    seats: list[AvailabilitySeat]


# ── Orders ──────────────────────────────────────────────────────────────────
class OrderCreate(BaseModel):
    seat_uids: list[str] = Field(
        min_length=1,
        description="All-or-nothing. If any seat is unavailable the whole call is a 409 and nothing is held.",
        examples=[["A-1", "A-2"]],
    )
    channel: Literal["marketplace", "api", "box_office", "comp", "invitation"] = "api"
    external_ref: str | None = Field(
        None,
        description=(
            "Your idempotency key, unique within your organization. Sending the "
            "same key twice returns the order the first call created instead of "
            "holding a second set of seats."
        ),
    )
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None


class OrderOut(Orm):
    id: int
    organization_id: int
    performance_id: int
    channel: str
    status: str
    expires_at: UtcDatetime | None = Field(
        None,
        description="When this order's hold on every one of its seats lapses. Authoritative; there is no per-seat deadline.",
    )
    extended: bool = Field(description="Whether the single permitted extension has been used.")
    subtotal_minor: int
    fees_minor: int
    discount_minor: int
    total_minor: int
    currency: str
    customer_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None
    external_ref: str | None = None
    created_at: UtcDatetime


class OrderDetail(OrderOut):
    seat_uids: list[str] = Field(description="The seats this order currently holds.")


class IssuedTicket(BaseModel):
    ticket_id: int
    seat_uid: str
    credential: str = Field(
        description=(
            "The QR payload. Returned HERE AND NOWHERE ELSE - only its hash is "
            "stored, so it cannot be read back. Lost credentials are replaced "
            "with POST /v1/tickets/{id}/rotate-credential, not recovered."
        )
    )


class OrderCompleteOut(BaseModel):
    order_id: int
    status: str = "completed"
    total_minor: int
    currency: str
    tickets: list[IssuedTicket]


# ── Tickets ─────────────────────────────────────────────────────────────────
class TicketOut(Orm):
    id: int
    order_id: int
    organization_id: int
    performance_id: int
    performance_seat_id: int
    seat_uid: str | None = None
    seat_label: str | None = None
    status: str
    credential_version: int
    issued_at: UtcDatetime
    checked_in_at: UtcDatetime | None = None
    checked_in_by_user_id: int | None = None
    amount_paid_minor: int
    currency: str


class TicketActionRequest(BaseModel):
    reason: str | None = Field(
        None, description="Recorded on the audit row. Free text, for humans."
    )


class RotateCredentialOut(BaseModel):
    ticket_id: int
    credential_version: int
    credential: str = Field(description="The new QR payload. Shown once; every earlier token is now `superseded`.")


# ── Check-in ────────────────────────────────────────────────────────────────
class CheckInRequest(BaseModel):
    credential: str = Field(description="The exact string encoded in the QR code.")
    performance_id: int | None = Field(
        None,
        description=(
            "The performance the scanner is working. Supply it: without it a "
            "ticket for tomorrow's show scans as `valid` at tonight's door."
        ),
    )


class CheckInOut(BaseModel):
    verdict: Literal[
        "valid",
        "already_checked_in",
        "cancelled",
        "refunded",
        "wrong_performance",
        "invalid",
        "superseded",
    ]
    message: str
    ticket_id: int | None = None
    performance_id: int | None = None
    seat_uid: str | None = None
    seat_label: str | None = None
    checked_in_at: UtcDatetime | None = None


# ── API keys ────────────────────────────────────────────────────────────────
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Box office iPad"])
    environment: Literal["production", "sandbox"] = Field(
        "sandbox",
        description="`production` mints a `ksk_live_...` key; `sandbox` mints `ksk_test_...`.",
    )
    scopes: list[Literal["read", "write"]] = Field(
        default_factory=lambda: ["read"],
        description="`write` implies `read` and is stored that way.",
    )


class ApiKeyOut(Orm):
    id: int
    organization_id: int
    name: str
    key_prefix: str = Field(description="The visible handle, e.g. `ksk_live_a1b2c3d4`. Not a secret.")
    environment: str
    scopes: list[str]
    last_used_at: UtcDatetime | None = None
    revoked_at: UtcDatetime | None = None
    created_at: UtcDatetime


class ApiKeyCreatedOut(ApiKeyOut):
    key: str = Field(
        description=(
            "The full key. Returned by this call and never again - only its hash "
            "is stored. Put it straight into your secret store."
        )
    )
