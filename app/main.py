"""Kursi.io API — FastAPI application entry point."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, engine
from app.routers import ai, bookings, events, refunds, users
from app.schemas import HealthResponse

logger = logging.getLogger("kursi")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables on startup. For real schema changes use Alembic."""
    Base.metadata.create_all(bind=engine)
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
