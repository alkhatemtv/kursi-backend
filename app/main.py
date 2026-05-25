"""Kursi.io API — FastAPI application entry point."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app.config import settings
from app.database import Base, engine
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
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Logged once at startup. Other endpoints remain functional; the AI router
        # returns 503 per-request until the key is configured.
        logger.warning(
            "ANTHROPIC_API_KEY is not set — /api/ai/* endpoints will return 503 "
            "until it is configured in the environment."
        )
    yield


app = FastAPI(
    title="Kursi.io API",
    description="Backend for the Kursi.io event ticketing platform.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_model=HealthResponse, tags=["meta"])
def root():
    return HealthResponse(status="ok", env=settings.app_env)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health():
    return HealthResponse(status="ok", env=settings.app_env)


app.include_router(users.router)
app.include_router(events.router)
app.include_router(bookings.router)
app.include_router(refunds.router)
app.include_router(ai.router)
app.include_router(wishlist.router)
