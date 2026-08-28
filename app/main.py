"""Kursi.io API — FastAPI application entry point."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app.api import v1 as api_v1
from app.api.errors import install_error_handlers
from app.config import settings
from app.database import Base, engine
from app.migrations import check_migration_state, get_app_version, log_migration_state
from app.routers import ai, bookings, events, refunds, users, wishlist
from app.schemas import HealthResponse

logger = logging.getLogger("kursi")


# Columns added in Phase 2 — keep this list small; for anything bigger, switch to Alembic.
_EVENT_PHASE2_COLUMNS: list[tuple[str, str]] = [
    ("performer", "TEXT"),
    ("gallery", "TEXT DEFAULT '[]'"),
    ("duration_minutes", "INTEGER"),
    ("view_count", "INTEGER DEFAULT 0 NOT NULL"),
    ("min_price", "REAL"),
]


def _migrate_sqlite_inplace() -> None:
    """Add any missing Phase-2 columns to an existing SQLite events table.

    `Base.metadata.create_all` creates new tables but never alters existing ones,
    so without this an old kursi.db would 500 the moment we touched a new column.
    Postgres / other DBs should use Alembic instead.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "events" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("events")}
    missing = [(n, t) for n, t in _EVENT_PHASE2_COLUMNS if n not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for name, coltype in missing:
            conn.execute(text(f"ALTER TABLE events ADD COLUMN {name} {coltype}"))
            logger.info("Added column events.%s", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables on startup. For real schema changes use Alembic."""
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_inplace()
    # Safety check only - logs drift between the DB schema revision and the code's
    # head revision. Never raises, never blocks startup. See app/migrations.py.
    log_migration_state(engine)
    logger.info("Starting Kursi API (env=%s, version=%s)", settings.environment, get_app_version())
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Logged once at startup. Other endpoints remain functional; the AI router
        # returns 503 per-request until the key is configured.
        logger.warning(
            "ANTHROPIC_API_KEY is not set — /api/ai/* endpoints will return 503 "
            "until it is configured in the environment."
        )
    yield


#: The blurb at the top of the generated docs. This is the first thing an
#: integrator reads, so it carries the two conventions that break clients when
#: they are guessed wrong: money and time.
API_DESCRIPTION = """
Backend for the Kursi.io event ticketing platform.

## Two APIs live here

* **`/v1/…` — the Kursi Engine.** The versioned, documented API: organizations,
  venues, seating layouts, events, performances, seat maps, checkout, tickets
  and check-in. Everything below is about this one.
* **everything else** — the original marketplace endpoints, frozen. They are
  unversioned, are not part of the public contract, and will not change.

## Authentication

Every `/v1` endpoint takes `Authorization: Bearer <credential>`, where the
credential is either an **Auth0 access token** (a person, acting through the
dashboard) or an **API key** (a machine, acting for one organization). They are
told apart by prefix — an API key starts `ksk_live_` (production) or `ksk_test_`
(sandbox). Each endpoint states which it accepts and what role or scope it
needs.

`GET /v1/me` is where a client starts: it returns the organizations you may act
for, and their ids are what the rest of the paths are built from.

## Money is always integer minor units

Every monetary field is named `*_minor` and is an **integer** count of the
currency's smallest unit, alongside a 3-letter ISO `currency`:

| currency | minor digits | `amount_minor` | means |
|---|---|---|---|
| KWD | 3 | `25000` | KWD 25.000 |
| KWD | 3 | `5500` | KWD 5.500 |
| USD | 2 | `1299` | USD 12.99 |

**Decimals and floats are rejected with 422.** `25.0` is not 25 dinars, and
accepting it would mean silently rounding somebody's revenue. Convert
explicitly before you send.

## Time is always UTC

Every timestamp is ISO-8601 with an explicit `+00:00` offset, and every deadline
in this API — a hold's `expires_at` above all — is judged by comparing
timestamps against the database's own clock. Nothing sweeps; a hold is dead the
microsecond it passes.

## Errors

Every `/v1` failure has the same shape:

```json
{"error": "seats_unavailable", "message": "seats unavailable: A-12", "detail": {}}
```

`error` is a stable machine string and is the thing to branch on; `message` is
for humans and may be reworded. Seat conflicts additionally carry a `conflicts`
array with one entry per offending seat — see
`POST /v1/performances/{id}/orders`.
"""

app = FastAPI(
    title="Kursi.io API",
    description=API_DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=api_v1.OPENAPI_TAGS,
)

# /v1 error envelope. Registered app-wide because FastAPI has no per-router
# exception handlers - every handler checks the request path and hands anything
# that is not /v1 straight back to FastAPI's default, so the frozen marketplace
# responses are unchanged. See app/api/errors.py.
install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _health_payload() -> HealthResponse:
    """Build the health payload. Always reports status 'ok' if the process can
    answer at all - migration drift is surfaced via `migration_state`, not by
    failing the check, so a lagging schema doesn't take the service out of a
    load balancer."""
    info = check_migration_state(engine)
    return HealthResponse(
        status="ok",
        env=settings.environment,
        version=get_app_version(),
        db_revision=info["db_revision"],
        head_revision=info["head_revision"],
        migration_state=info["state"],
    )


@app.get("/", response_model=HealthResponse, tags=["meta"])
def root():
    return _health_payload()


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health():
    return _health_payload()


app.include_router(api_v1.router)

app.include_router(users.router)
app.include_router(events.router)
app.include_router(bookings.router)
app.include_router(refunds.router)
app.include_router(ai.router)
app.include_router(wishlist.router)
