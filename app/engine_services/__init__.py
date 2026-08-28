"""Kursi Engine domain services (Phase 1b) - see docs/engine-phase1-schema-spec.md.

Phase 1a put the schema and its DB-enforced invariants in place. This package is
the business logic that sits on top of them, and nothing else: there are no
routes here (those are Phase 1c), no HTTP types, and no imports from FastAPI.
Everything takes a `Session` and returns ORM objects or small result dataclasses,
so the same functions serve the marketplace, the public API and the box office
without a translation layer.

    clock          the injectable source of "now"; DB-backed in production
    uow            one public call == one transaction
    errors         structured, coded failures a caller can act on
    audit          append-only writes (spec 6)
    availability   THE seat availability predicate, defined once
    layout         reading layout_data into inventory records
    pricing        integer minor units, override-beats-category
    provisioning   personal org + owner membership on first login (spec 1)
    publishing     layout freeze + inventory materialisation (spec 2/3)
    locking        holds, extension, release, GC (spec 4, Decision 3)
    fulfilment     completion -> tickets + usage, cancel/refund (spec 5/6)

Read `locking` first if you are here to understand the design; it carries the
reasoning for the whole checkout path.
"""
from app.engine_services.availability import (  # noqa: F401
    LIVE_ORDER_STATUSES,
    LIVE_TICKET_STATUSES,
    PUBLIC_SEAT_STATUSES,
    SELLABLE_SEAT_STATUS,
    available_seat_uids,
    describe_unavailable,
    is_seat_available,
    public_seat_status,
    seat_availability_rows,
    seat_is_available_expr,
)
from app.engine_services.clock import (  # noqa: F401
    Clock,
    DatabaseClock,
    ManualClock,
    as_utc,
    get_clock,
    set_clock,
    using_clock,
)
from app.engine_services.credentials import (  # noqa: F401
    Credential,
    credential_hash,
    issue_credential,
    verify_credential,
)
from app.engine_services.errors import (  # noqa: F401
    ConcurrentPublish,
    EngineConflict,
    EngineServiceError,
    ExtensionAlreadyUsed,
    InvalidTicketTransition,
    LayoutInvalid,
    NotFound,
    OrderNotLive,
    PricingUnavailable,
    SeatConflict,
    SeatsUnavailable,
    ValidationError,
)
from app.engine_services.fulfilment import (  # noqa: F401
    CompletionResult,
    RotationResult,
    cancel_ticket,
    check_in_ticket,
    complete_order,
    live_ticket_for_seat,
    order_tickets,
    refund_ticket,
    rotate_credential,
)
from app.engine_services.layout import (  # noqa: F401
    LayoutCategory,
    LayoutSeat,
    ParsedLayout,
    parse_layout,
)
from app.engine_services.locking import (  # noqa: F401
    DEFAULT_HOLD_MINUTES,
    EXTENSION_MINUTES,
    GcResult,
    create_draft_order,
    extend_order,
    gc_expired_locks,
    release_order,
)
from app.engine_services.pricing import (  # noqa: F401
    DEFAULT_CURRENCY,
    normalize_prices,
    price_for_seat,
)
from app.engine_services.provisioning import (  # noqa: F401
    ensure_personal_organization,
    find_active_organization,
    personal_org_slug,
    slugify,
)
from app.engine_services.publishing import (  # noqa: F401
    PublishResult,
    publish_performance,
)
from app.engine_services.uow import unit_of_work  # noqa: F401

__all__ = [
    "Clock",
    "CompletionResult",
    "ConcurrentPublish",
    "Credential",
    "DEFAULT_CURRENCY",
    "DEFAULT_HOLD_MINUTES",
    "DatabaseClock",
    "EXTENSION_MINUTES",
    "EngineConflict",
    "EngineServiceError",
    "ExtensionAlreadyUsed",
    "GcResult",
    "InvalidTicketTransition",
    "LIVE_ORDER_STATUSES",
    "LIVE_TICKET_STATUSES",
    "LayoutCategory",
    "LayoutInvalid",
    "LayoutSeat",
    "ManualClock",
    "NotFound",
    "OrderNotLive",
    "PUBLIC_SEAT_STATUSES",
    "ParsedLayout",
    "PricingUnavailable",
    "PublishResult",
    "RotationResult",
    "SELLABLE_SEAT_STATUS",
    "SeatConflict",
    "SeatsUnavailable",
    "ValidationError",
    "as_utc",
    "available_seat_uids",
    "cancel_ticket",
    "check_in_ticket",
    "complete_order",
    "create_draft_order",
    "credential_hash",
    "describe_unavailable",
    "ensure_personal_organization",
    "extend_order",
    "find_active_organization",
    "gc_expired_locks",
    "get_clock",
    "is_seat_available",
    "issue_credential",
    "live_ticket_for_seat",
    "normalize_prices",
    "order_tickets",
    "parse_layout",
    "personal_org_slug",
    "price_for_seat",
    "public_seat_status",
    "publish_performance",
    "refund_ticket",
    "release_order",
    "rotate_credential",
    "seat_availability_rows",
    "seat_is_available_expr",
    "set_clock",
    "slugify",
    "unit_of_work",
    "using_clock",
    "verify_credential",
]
