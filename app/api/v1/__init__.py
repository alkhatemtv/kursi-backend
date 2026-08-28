"""The /v1 router.

Route order matters here in exactly one place: `venues.router` and
`events.router` both mount under `/orgs/{org_id}`, and FastAPI matches in
registration order. Their paths do not overlap, so the order below is for
readability - resources first, then the things that act on them.
"""
from fastapi import APIRouter

from app.api.v1 import (
    checkin,
    events,
    me,
    orders,
    organizations,
    performances,
    tickets,
    venues,
)

#: Tag descriptions, surfaced as section blurbs in the generated docs. These are
#: read by integrators before any endpoint is, so they carry the conventions
#: that apply across a whole group rather than restating them per route.
OPENAPI_TAGS: list[dict] = [
    {
        "name": "identity",
        "description": "Who you are and which organizations you may act for.",
    },
    {
        "name": "organizations",
        "description": (
            "The tenant. Everything else in this API hangs off an organization, "
            "and every credential is scoped to exactly one."
        ),
    },
    {
        "name": "api-keys",
        "description": (
            "Machine credentials. A key is shown once at creation and stored "
            "only as a hash; `ksk_live_…` acts on the production environment "
            "and `ksk_test_…` on sandbox. Managing keys requires a user token — "
            "a key cannot mint another key."
        ),
    },
    {
        "name": "venues",
        "description": "Physical places. Seating lives in the layouts beneath them.",
    },
    {
        "name": "layouts",
        "description": (
            "Seating arrangements, as immutable versions. A version is frozen "
            "the first time a performance materialises inventory from it and is "
            "never unfrozen — editing means creating the next version. That rule "
            "is a database trigger, not a convention."
        ),
    },
    {
        "name": "events",
        "description": "The show. Its dated showings are performances.",
    },
    {
        "name": "performances",
        "description": (
            "A dated showing, which owns its inventory. Publishing freezes the "
            "layout version, materialises seats and sets prices in one "
            "transaction."
        ),
    },
    {
        "name": "availability",
        "description": (
            "The seat map. Readable without a credential for a performance that "
            "is on sale, because that is public information."
        ),
    },
    {
        "name": "checkout",
        "description": (
            "Holding seats and turning a hold into tickets. Holds are eight "
            "minutes, extendable once by four, and expire by timestamp "
            "comparison — no job runs, and a seat is sellable again the "
            "microsecond its hold lapses."
        ),
    },
    {
        "name": "tickets",
        "description": (
            "Issued tickets and what happens to them afterwards. A ticket's id "
            "is stable for life; its QR credential can be rotated independently."
        ),
    },
    {
        "name": "check-in",
        "description": "The door. One call resolves a QR and admits the holder.",
    },
]

router = APIRouter(prefix="/v1")
router.include_router(me.router)
router.include_router(organizations.router)
router.include_router(venues.router)
router.include_router(events.router)
router.include_router(performances.router)
router.include_router(orders.router)
router.include_router(tickets.router)
router.include_router(checkin.router)

__all__ = ["OPENAPI_TAGS", "router"]
