"""Fixtures for the Phase 1b service tests.

WHY A SEPARATE DATABASE FROM tests/conftest.py
----------------------------------------------
Same reason `test_engine_schema.py` builds its own: the invariants these
services lean on - two partial unique indexes and the layout freeze trigger -
exist only in the Alembic migration, and `Base.metadata.create_all()` does not
create triggers. A locking test that ran against a `create_all` database would
be testing application code against a schema production does not have. So this
package runs `alembic upgrade head` against a dedicated database and works
there.

WHY THE SQLite PRAGMAS
----------------------
Two of these tests use real threads. Out of the box, pysqlite opens DEFERRED
transactions, so two connections can both start reading and then both try to
upgrade to a write - a case SQLite refuses immediately with "database is
locked" rather than waiting, no matter what `busy_timeout` says. The connect /
begin hooks below take the write lock up front (`BEGIN IMMEDIATE`) and wait for
it (`busy_timeout`), which makes concurrent writers *queue* instead of erroring
out spuriously.

This is a harness setting, not a behaviour change: it does not make SQLite
capable of the interleaved-transaction contention PostgreSQL has, and no test
here pretends otherwise (see `test_locking.py`, which is explicit about which
race each test does and does not prove).

`foreign_keys=ON` is set as well - SQLite ignores foreign keys by default, and
these services rely on FK RESTRICT meaning something.
"""
from __future__ import annotations

import os
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app import engine_models as em
from app.engine_services import clock as engine_clock
from app.models import User
from tests.engine.layouts import (
    GRID_COLS,
    GRID_ROWS,
    PRICES,
    T0,
    TOTAL_SEATS,
    make_layout_data,
)

ROOT = Path(__file__).resolve().parent.parent.parent

_TEST_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
IS_POSTGRES = _TEST_URL.startswith("postgresql") or _TEST_URL.startswith("postgres://")

postgres_only = pytest.mark.skipif(
    not IS_POSTGRES,
    reason=(
        "Requires PostgreSQL's concurrency model: overlapping write transactions "
        "with row-level locking. SQLite serialises writers, so this scenario "
        "cannot be expressed there and is NOT faked with sequential calls. Set "
        "TEST_DATABASE_URL to a scratch PostgreSQL database to run it - see "
        "TESTING.md."
    ),
)

#: Everything the services can write, children first.
WIPE_ORDER = (
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
    User,
)


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.cmd_opts = Namespace(x=[f"db_url={url}"])
    return cfg


# ── Database ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def engine_url(tmp_path_factory) -> str:
    """A database at head, built by the real migration."""
    if IS_POSTGRES:
        cfg = _alembic_config(_TEST_URL)
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        return _TEST_URL

    db_file = tmp_path_factory.mktemp("engine_services") / "engine.db"
    url = f"sqlite:///{db_file}"
    command.upgrade(_alembic_config(url), "head")
    return url


@pytest.fixture(scope="session")
def db_engine(engine_url):
    eng = create_engine(engine_url, future=True)

    if eng.dialect.name == "sqlite":

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - setup
            # Hand transaction control to SQLAlchemy so the "begin" hook below
            # can choose the locking mode.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(eng, "begin")
        def _begin_immediate(connection):  # pragma: no cover - setup
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def session_factory(db_engine):
    return sessionmaker(bind=db_engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def clean_database(session_factory):
    """Empty every table before each test.

    These services COMMIT - that is the whole point of a durable hold - so
    rollback-per-test isolation is not available here. Wiping is.
    """
    cleaner = session_factory()
    try:
        for model in WIPE_ORDER:
            cleaner.query(model).delete()
        cleaner.commit()
    finally:
        cleaner.close()
    yield


@pytest.fixture
def session(session_factory) -> Session:
    s = session_factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def new_session(session_factory):
    """Factory for extra sessions - a second actor, or a worker thread."""
    created: list[Session] = []

    def _make() -> Session:
        s = session_factory()
        created.append(s)
        return s

    yield _make
    for s in created:
        s.rollback()
        s.close()


# ── Clock ───────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def manual_clock():
    """Every engine test runs on a hand-driven clock starting at T0.

    Installed for ALL of them, not just the expiry ones: a service that read the
    wall clock somewhere it should have read the injected one would otherwise
    pass here and fail in production.
    """
    clock = engine_clock.ManualClock(T0)
    previous = engine_clock.set_clock(clock)
    yield clock
    engine_clock.set_clock(previous)


# ── Object graph ────────────────────────────────────────────────────────────
@pytest.fixture
def world(session):
    """org -> venue -> layout -> draft layout_version(144 seats) -> event ->
    performance. Nothing is published yet."""
    user = User(
        auth0_sub="test|engine-owner",
        email="owner@kursi.io",
        name="Engine Owner",
        role="organizer",
    )
    session.add(user)
    session.flush()

    org = em.Organization(name="Kursi Events", slug="kursi-events", type="business")
    session.add(org)
    session.flush()

    venue = em.Venue(organization_id=org.id, name="Main Theatre")
    session.add(venue)
    session.flush()

    layout = em.VenueLayout(venue_id=venue.id, name="Main Hall - Full")
    session.add(layout)
    session.flush()

    version = em.LayoutVersion(
        venue_layout_id=layout.id,
        version_number=1,
        status="draft",
        created_by_user_id=user.id,
        layout_data=make_layout_data(),
    )
    session.add(version)
    session.flush()

    event = em.EngineEvent(
        organization_id=org.id, venue_id=venue.id, title="Test Event", status="active"
    )
    session.add(event)
    session.flush()

    performance = em.Performance(
        event_id=event.id,
        layout_version_id=version.id,
        starts_at=T0 + timedelta(days=30),
        status="draft",
    )
    session.add(performance)
    session.flush()
    session.commit()

    return {
        "user_id": user.id,
        "org_id": org.id,
        "venue_id": venue.id,
        "layout_id": layout.id,
        "version_id": version.id,
        "event_id": event.id,
        "performance_id": performance.id,
    }


@pytest.fixture
def published(session, world):
    """`world`, with the layout frozen and 144 seats materialised and priced."""
    from app.engine_services import publish_performance

    publish_performance(
        session,
        world["performance_id"],
        prices=PRICES,
        actor_user_id=world["user_id"],
    )
    session.commit()
    return world
