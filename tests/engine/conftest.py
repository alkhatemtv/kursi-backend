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
from sqlalchemy import text as sa_text
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
    """A database at head, built by the real migration.

    ON SQLite the two suites do not collide: this package gets its own temp
    file, and `tests/conftest.py` keeps its own.

    ON PostgreSQL they share ONE database - `TEST_DATABASE_URL` is a single URL -
    and they build a schema in two incompatible ways. `tests/conftest.py` calls
    `Base.metadata.create_all()`; this package runs the Alembic migration. The
    root conftest's session fixture runs first, so by the time this one starts,
    `users` and every `engine_` table already exist with no `alembic_version` row
    to show for it. `alembic downgrade base` then has nothing recorded to undo
    and `upgrade head` walks straight into `relation "users" already exists`.

    So on PostgreSQL the slate is wiped for real - schema and all - before the
    migration runs. That makes this package's contract ("I own this database and
    I build it with the migration") literally true rather than nearly true. The
    root suite is not harmed: the baseline revision creates the legacy tables
    it needs, identically to `create_all`, and every test wipes its own rows on
    setup anyway.
    """
    if IS_POSTGRES:
        scratch = create_engine(_TEST_URL, future=True)
        try:
            with scratch.begin() as conn:
                conn.execute(sa_text("DROP SCHEMA public CASCADE"))
                conn.execute(sa_text("CREATE SCHEMA public"))
        finally:
            scratch.dispose()
        command.upgrade(_alembic_config(_TEST_URL), "head")
        return _TEST_URL

    db_file = tmp_path_factory.mktemp("engine_services") / "engine.db"
    url = f"sqlite:///{db_file}"
    command.upgrade(_alembic_config(url), "head")
    return url


@pytest.fixture(scope="session")
def db_engine(engine_url):
    # `pool_pre_ping` matters only for the remote case: against the staging
    # server every pooled connection crosses the public internet through a TCP
    # proxy that is entitled to drop an idle one, and a connection that died
    # between two tests would otherwise surface as a fixture ERROR that has
    # nothing to do with the code under test. The ping costs one round trip on
    # checkout and is free on SQLite, which never loses a connection.
    eng = create_engine(engine_url, future=True, pool_pre_ping=True)

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
def clean_database(session_factory, db_engine):
    """Empty every table before each test.

    These services COMMIT - that is the whole point of a durable hold - so
    rollback-per-test isolation is not available here. Wiping is.

    On PostgreSQL that wipe is ONE statement rather than eighteen. It is the
    same eighteen tables either way, but the suite is also run against the
    staging server over a public TCP proxy, where every statement is a network
    round trip: eighteen per test across the whole suite is thousands of
    round trips spent deleting nothing. `TRUNCATE ... CASCADE` collapses them
    into one and, unlike `DELETE`, does not have to scan each table first.
    Identity sequences are deliberately NOT restarted, so ids keep behaving
    exactly as they do under the per-table deletes SQLite still uses.
    """
    if db_engine.dialect.name == "postgresql":
        tables = ", ".join(m.__tablename__ for m in WIPE_ORDER)
        with db_engine.begin() as conn:
            conn.execute(sa_text(f"TRUNCATE TABLE {tables} CASCADE"))
        yield
        return

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


# ── /v1 API harness (Phase 1c) ──────────────────────────────────────────────
# The route tests run against THIS database, not the one `tests/conftest.py`
# builds. Two of the things /v1 must get right - the frozen-layout 409 and the
# never-double-sell backstop - exist only in the Alembic migration, and
# `Base.metadata.create_all()` creates neither. A route test on a `create_all`
# database would be asserting against a schema production does not have.
#
# The seam is `app.database.get_db`: overriding it hands every request a session
# from the same migrated engine the service fixtures use, which also means the
# autouse `manual_clock` above governs route tests too - expiry is driven, not
# waited for.

#: Marker consumed by the patched `_decode_token`. Not a real JWT; nothing in
#: this suite ever contacts Auth0.
API_TOKEN_PREFIX = "FAKE."

#: Every role in spec 1, each given its own user in `api_world`, so an
#: authorisation test can name a role instead of constructing one.
API_ROLE_SUBS = {role: f"test|role-{role}" for role in em.MEMBERSHIP_ROLES}


def api_token(sub: str, email: str | None = None) -> str:
    return f"{API_TOKEN_PREFIX}{sub}.organizer.{email or sub.replace('|', '_')}@kursi.io"


def user_header(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_token(sub)}"}


def role_header(role: str) -> dict[str, str]:
    return user_header(API_ROLE_SUBS[role])


def key_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api_client(session_factory, monkeypatch):
    """A TestClient whose requests run against the migrated engine database."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from app import auth as app_auth
    from app.database import get_db
    from app.main import app

    def _decode(token: str) -> dict:
        if not token.startswith(API_TOKEN_PREFIX):
            raise HTTPException(status_code=401, detail="Invalid token")
        _, sub, role, email = token.split(".", 3)
        return {
            "sub": sub,
            "email": email,
            "https://kursi.io/role": role,
            "https://kursi.io/name": sub,
        }

    monkeypatch.setattr(app_auth, "_decode_token", _decode)

    def _override_get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def api_world(session, world):
    """`world`, plus a user holding each spec-1 role in that organization, plus
    a SECOND organization with its own owner for cross-tenant assertions.

    Built in two flushes rather than one per row. On SQLite that is invisible;
    against the staging PostgreSQL over a public proxy every flush is a network
    round trip, and this fixture runs before every route test.
    """
    from app.models import User as LegacyUser

    users = {
        role: LegacyUser(
            auth0_sub=sub,
            email=f"{sub.replace('|', '_')}@kursi.io",
            name=role,
            role="organizer",
        )
        for role, sub in API_ROLE_SUBS.items()
    }
    # A member whose invitation was never accepted, and someone from elsewhere.
    users["_invited"] = LegacyUser(
        auth0_sub="test|invited", email="invited@kursi.io", name="Invited",
        role="organizer",
    )
    users["_outsider"] = LegacyUser(
        auth0_sub="test|outsider", email="outsider@rival.example",
        name="Outsider", role="organizer",
    )
    other_org = em.Organization(name="Rival Ltd", slug="rival-ltd", type="business")
    session.add_all([*users.values(), other_org])
    session.flush()

    memberships = [
        em.Membership(
            organization_id=world["org_id"], user_id=users[role].id,
            role=role, status="active",
        )
        for role in API_ROLE_SUBS
    ]
    memberships.append(
        # 'invited' authorises nothing - it is not an active membership.
        em.Membership(
            organization_id=world["org_id"], user_id=users["_invited"].id,
            role="owner", status="invited",
        )
    )
    memberships.append(
        em.Membership(
            organization_id=other_org.id, user_id=users["_outsider"].id,
            role="owner", status="active",
        )
    )
    session.add_all(memberships)
    session.flush()

    # Read the ids BEFORE committing. `expire_on_commit` is on, so touching
    # `user.id` afterwards would issue a refresh - which on SQLite opens a fresh
    # BEGIN IMMEDIATE that nothing then commits, and the first request to write
    # would sit on that lock until `busy_timeout` gave up.
    world.update({f"user_{role}": users[role].id for role in API_ROLE_SUBS})
    world["user_invited"] = users["_invited"].id
    world["user_outsider"] = users["_outsider"].id
    world["other_org_id"] = other_org.id

    session.commit()
    return world


@pytest.fixture
def make_api_key(session):
    """Mint a real API key row and hand back the token, as the endpoint would."""
    from app.api import keys as api_keys

    def _make(
        organization_id: int,
        *,
        scopes: list[str] | None = None,
        environment: str = "sandbox",
        revoked: bool = False,
        name: str = "test key",
    ) -> str:
        token, key_prefix, key_hash = api_keys.mint(environment)
        row = em.ApiKey(
            organization_id=organization_id,
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            environment=environment,
            scopes=api_keys.normalize_scopes(scopes or ["read"]),
        )
        if revoked:
            row.revoked_at = T0
        session.add(row)
        session.commit()
        return token

    return _make
