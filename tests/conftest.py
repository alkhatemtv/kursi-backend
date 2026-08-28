"""Shared pytest fixtures + the guarantee that tests can never touch production.

READ THIS FIRST - how production isolation works
------------------------------------------------
`app.database` builds its engine at import time from `settings.database_url`.
So the only reliable way to pin the test database is to set `DATABASE_URL` in the
environment *before* anything under `app.` is imported. pytest imports conftest.py
before it imports any test module, which makes this file the right place.

Three layers of protection, in order:
  1. Whatever `DATABASE_URL` happens to be in the shell (or in a local `.env`) is
     discarded outright. Tests never inherit it.
  2. The replacement URL is either a throwaway SQLite file or an explicit
     `TEST_DATABASE_URL`. Anything else is refused.
  3. The resulting URL is pattern-matched against known Railway/production hosts
     and the process aborts if it looks live - a belt-and-braces check in case
     someone points TEST_DATABASE_URL at the real database by mistake.

Layer 3 fires before a single table is created, so a misconfigured run stops
rather than dropping production tables.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Hosts/substrings that indicate a real deployed database. Matching any of these
# is a hard stop.
_FORBIDDEN_URL_MARKERS = (
    "rlwy.net",           # Railway public TCP proxy
    "railway.internal",   # Railway private network
    "railway.app",
    "amazonaws.com",
    "supabase.co",
    "neon.tech",
)

# ── The one way past layer 3 ────────────────────────────────────────────────
# Some tests can only run on real PostgreSQL - the concurrency races, the
# plpgsql freeze guard, TEXT[] - and the only PostgreSQL this project has is on
# Railway, behind a host layer 3 refuses on sight. Refusing outright would mean
# those tests never run anywhere, which is worse than the risk being guarded
# against; refusing quietly would be worse still.
#
# So there is exactly one deliberate way through, and it is loud:
#
#   1. `ALLOW_REMOTE_TEST_DATABASE` must be set to the exact phrase below. A
#      value nobody types by accident, and nothing that could be exported once
#      and forgotten in a shell profile without being noticed.
#   2. The target database must NOT be called `railway`. That is the name
#      Railway gives the database it provisions for a service, in EVERY
#      environment - so this rule means the escape hatch can only ever point at
#      a scratch database somebody created on purpose. Aiming it at production,
#      or at staging's own database, is still refused.
#   3. The host and database are printed before a single table is created, so a
#      mistake is visible in the test output rather than discovered afterwards.
#
# `alembic downgrade base` runs against whatever this resolves to. Treat it as
# a database you are willing to lose, because that is what it becomes.
_REMOTE_OPT_IN_ENV = "ALLOW_REMOTE_TEST_DATABASE"
_REMOTE_OPT_IN_PHRASE = "i-understand-this-drops-tables"
_RESERVED_DB_NAMES = ("railway",)


def _remote_opt_in_granted(url: str) -> bool:
    """Is the loud, deliberate opt-in present AND aimed somewhere disposable?"""
    if os.environ.get(_REMOTE_OPT_IN_ENV, "").strip() != _REMOTE_OPT_IN_PHRASE:
        return False

    from urllib.parse import urlparse

    parsed = urlparse(url)
    database = (parsed.path or "").lstrip("/").split("?")[0]
    if database.lower() in _RESERVED_DB_NAMES:
        raise RuntimeError(
            f"REFUSING TO RUN TESTS: {_REMOTE_OPT_IN_ENV} is set, but the target "
            f"database is named {database!r} - the name Railway gives the "
            f"database it provisions for a service, in every environment. The "
            f"suite DROPS AND RECREATES every table it touches. Create a scratch "
            f"database on that server (e.g. `kursi_scratch`) and point "
            f"TEST_DATABASE_URL at that instead."
        )
    banner = [
        "==============================================================================",
        "  REMOTE TEST DATABASE IN USE - tables will be DROPPED and recreated",
        f"    host     {parsed.hostname}:{parsed.port}",
        f"    database {database}",
        "==============================================================================",
    ]
    print(("\n".join(banner)), flush=True)
    return True


TEST_DB_FILE = ROOT / "test_kursi_suite.db"


def _resolve_test_database_url() -> str:
    """Pick the test database URL and refuse anything that could be production."""
    explicit = (os.environ.get("TEST_DATABASE_URL") or "").strip()

    if explicit:
        url = explicit
    else:
        # Why a file and not `sqlite:///:memory:` - a pure in-memory SQLite database
        # is per-connection, so the TestClient's request threads would each get their
        # own empty database. A temp file gives every connection the same schema and
        # is deleted at the end of the session.
        if TEST_DB_FILE.exists():
            TEST_DB_FILE.unlink()
        url = f"sqlite:///{TEST_DB_FILE}"

    lowered = url.lower()

    matched = next((m for m in _FORBIDDEN_URL_MARKERS if m in lowered), None)
    if matched and not _remote_opt_in_granted(url):
        raise RuntimeError(
            f"REFUSING TO RUN TESTS: the resolved test database URL points at "
            f"what looks like a live database (matched {matched!r}). "
            f"Tests must never run against production. "
            f"Unset TEST_DATABASE_URL to use the default throwaway SQLite file, "
            f"or see the {_REMOTE_OPT_IN_ENV} contract in this file if you "
            f"genuinely need a remote scratch database."
        )

    if not lowered.startswith("sqlite"):
        # A non-SQLite URL is allowed, but only when it was requested deliberately.
        if not explicit:
            raise RuntimeError(
                "REFUSING TO RUN TESTS: non-SQLite database URL resolved without an "
                "explicit TEST_DATABASE_URL. See TESTING.md."
            )

    return url


# ── Environment pinning - must happen before importing app.* ────────────────
os.environ.pop("DATABASE_URL", None)
os.environ["DATABASE_URL"] = _resolve_test_database_url()
# Deterministic settings so a stray value in the developer's shell cannot leak in.
os.environ["ENV"] = "development"
os.environ["APP_ENV"] = "development"
os.environ["AUTH0_DOMAIN"] = "test.auth0.com"
os.environ["AUTH0_API_AUDIENCE"] = "https://api.kursi.io"
os.environ["AUTH0_NAMESPACE"] = "https://kursi.io/"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text as sa_text  # noqa: E402

from app import auth  # noqa: E402
from app import engine_models as em  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Booking, Event, Refund, User, Wishlist  # noqa: E402

# Engine tables, children first, for the per-test wipe below. Phase 1b made
# `get_current_user` provision a personal organization, so every authenticated
# request in the legacy suites now leaves Engine rows behind. They have to go
# with the legacy rows: `engine_memberships.user_id` is an FK onto `users.id`
# with ON DELETE RESTRICT, so deleting users while a membership survives would
# fail outright on PostgreSQL - and on SQLite (FK enforcement off by default)
# would silently leave a membership pointing at a recycled user id.
_ENGINE_WIPE_ORDER = (
    em.WebhookDelivery,
    em.WebhookEndpoint,
    em.AuditLog,
    em.UsageEvent,
    em.Ticket,
    em.SeatLock,
    em.Order,
    em.PerformanceSeat,
    em.PerformanceCategory,
    em.Performance,
    em.EngineEvent,
    em.LayoutVersion,
    em.VenueLayout,
    em.Venue,
    em.ApiKey,
    em.Membership,
    em.Organization,
)

# Final assertion: the app really did pick up the test URL.
assert settings.database_url == os.environ["DATABASE_URL"], (
    "Settings did not pick up the test DATABASE_URL - aborting before touching any DB."
)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create the schema once for the whole session; drop it at the end."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if TEST_DB_FILE.exists():
        try:
            TEST_DB_FILE.unlink()
        except OSError:  # Windows may still hold the handle; harmless.
            pass


@pytest.fixture
def db():
    """A session against the test database, with all rows cleared first.

    Deletion order respects the foreign keys (children before parents), and
    covers the Engine tables as well as the legacy ones - see
    `_ENGINE_WIPE_ORDER`.
    """
    session = SessionLocal()
    if engine.dialect.name == "postgresql":
        # One statement instead of twenty-two. See the same note in
        # tests/engine/conftest.py: this suite is also run against a remote
        # PostgreSQL over a public proxy, where each statement is a round trip.
        tables = ", ".join(
            [m.__tablename__ for m in _ENGINE_WIPE_ORDER]
            + [m.__tablename__ for m in (Wishlist, Refund, Booking, Event, User)]
        )
        session.execute(sa_text(f"TRUNCATE TABLE {tables} CASCADE"))
        session.commit()
    else:
        for model in _ENGINE_WIPE_ORDER:
            session.query(model).delete()
        for model in (Wishlist, Refund, Booking, Event, User):
            session.query(model).delete()
        session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db):
    """FastAPI test client (httpx-backed) on a freshly emptied database."""
    with TestClient(app) as c:
        yield c


# ── Auth helpers ────────────────────────────────────────────────────────────
def make_token(sub: str = "test|user-1", role: str = "customer", email: str | None = None) -> str:
    """An opaque marker consumed by the patched `_decode_token` below.

    It is NOT a real JWT - nothing in the test suite ever contacts Auth0.
    """
    return f"FAKE.{sub}.{role}.{email or 'user@example.com'}"


def auth_header(sub: str = "test|user-1", role: str = "customer", email: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub, role, email)}"}


@pytest.fixture
def fake_jwt(monkeypatch):
    """Replace JWT verification at the dependency boundary.

    We patch `app.auth._decode_token`, which is the exact seam where
    `get_current_user` turns a bearer string into claims. Everything downstream -
    the user lookup, the auto-provisioning insert, the role sync - runs for real.
    Auth0's network calls (`_get_jwks`) are never reached.
    """
    from fastapi import HTTPException

    def _decode(token: str) -> dict:
        if not token.startswith("FAKE."):
            raise HTTPException(status_code=401, detail="Invalid token")
        _, sub, role, email = token.split(".", 3)
        return {
            "sub": sub,
            "email": email,
            "https://kursi.io/role": role,
            "https://kursi.io/name": email.split("@")[0],
        }

    monkeypatch.setattr(auth, "_decode_token", _decode)
    return _decode


# ── Seed data ───────────────────────────────────────────────────────────────
SEED_CATEGORIES = [
    {"id": "cat-vip", "name": "VIP", "price": 120.0, "color": "#c9a227"},
    {"id": "cat-std", "name": "Standard", "price": 45.0, "color": "#3b82f6"},
]


def _make_seats(rows: int = 2, cols: int = 3) -> list[dict]:
    seats = []
    for r in range(rows):
        for c in range(cols):
            seats.append(
                {
                    "id": f"s{r}-{c}",
                    "x": 100 + c * 40,
                    "y": 100 + r * 40,
                    "catId": "cat-vip" if r == 0 else "cat-std",
                    "row": r + 1,
                    "col": c + 1,
                    "label": f"{chr(65 + r)}{c + 1}",
                    "blocked": False,
                }
            )
    return seats


@pytest.fixture
def seed(db):
    """Minimal fixture data: one org-less user and one public event with seats.

    Returns the ORM objects so tests can reference ids without re-querying.
    """
    organizer = User(
        auth0_sub="test|organizer-1",
        email="organizer@example.com",
        name="Test Organizer",
        role="organizer",
        org_name=None,  # deliberately org-less
    )
    db.add(organizer)
    db.commit()
    db.refresh(organizer)

    event = Event(
        organizer_id=organizer.id,
        name="Seeded Test Event",
        description="An event created by the test fixtures.",
        venue="Test Hall",
        event_date="2030-01-15T20:00:00",
        icon="🎭",
        tag="Theater",
        stage_w=1400,
        stage_h=900,
        seats=_make_seats(),
        categories=SEED_CATEGORIES,
        performer="The Test Troupe",
        gallery=[],
        duration_minutes=90,
        min_price=45.0,
        status="active",
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    return {"user": organizer, "event": event}
