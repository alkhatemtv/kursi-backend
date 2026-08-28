"""Phase 1a: the DB-enforced invariants of the Kursi Engine schema.

WHY THESE TESTS BUILD THEIR OWN DATABASE
-----------------------------------------
The invariants under test (two partial unique indexes and the layout freeze trigger)
exist only in the Alembic migration - `Base.metadata.create_all()` does not create
triggers. The shared conftest database is built with `create_all`, and the legacy
suites drop/recreate it between tests, which would silently remove the triggers.

So this module builds a dedicated database by running the real migration
(`alembic upgrade head`) and runs every assertion against that. The invariants are
therefore tested exactly as production will have them.

WHICH BACKEND
-------------
`TEST_DATABASE_URL` set to a PostgreSQL URL  -> tests run against PostgreSQL.
Otherwise                                    -> a throwaway SQLite file.

SQLite turns out to support BOTH partial unique indexes and triggers, so none of
these tests are skipped by default - they genuinely execute on either backend. The
one place where semantics truly diverge (plpgsql, TEXT[]) is isolated in
`TestPostgresSpecificDDL`, which skips with a visible reason. See TESTING.md.
"""
from __future__ import annotations

import os
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import BigInteger, Integer, create_engine, inspect, text
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, StatementError
from sqlalchemy.orm import sessionmaker

from app import engine_models as em
from app.models import User

ROOT = Path(__file__).resolve().parent.parent

BASELINE_REVISION = "d29b3ede11f0"
PHASE1A_REVISION = "63446a371e5a"

_TEST_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
_IS_POSTGRES = _TEST_URL.startswith("postgresql") or _TEST_URL.startswith("postgres://")

postgres_only = pytest.mark.skipif(
    not _IS_POSTGRES,
    reason=(
        "Requires real PostgreSQL semantics (plpgsql / TEXT[]). Set TEST_DATABASE_URL "
        "to a scratch PostgreSQL database to run - see TESTING.md. These run against "
        "the staging database once that environment exists."
    ),
)


def _alembic_config(url: str) -> Config:
    """Alembic config pinned to one URL via the `-x db_url=` hook in alembic/env.py."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.cmd_opts = Namespace(x=[f"db_url={url}"])
    return cfg


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def migrated_url(tmp_path_factory) -> str:
    """A database at head, built by running the real migration."""
    if _IS_POSTGRES:
        url = _TEST_URL
        cfg = _alembic_config(url)
        # Rebuild from scratch so the trigger and partial indexes are the
        # migration's, not create_all's.
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    else:
        db_file = tmp_path_factory.mktemp("engine_schema") / "engine.db"
        url = f"sqlite:///{db_file}"
        command.upgrade(_alembic_config(url), "head")
    return url


@pytest.fixture(scope="module")
def migrated_engine(migrated_url):
    eng = create_engine(migrated_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(migrated_engine):
    """A session on the migrated database, rolled back after each test."""
    maker = sessionmaker(bind=migrated_engine, autocommit=False, autoflush=False)
    s = maker()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def fixture_ids(session):
    """Minimal Engine object graph: org -> venue -> layout -> version -> event ->
    performance -> one seat, plus an order. Returns the ids as a dict."""
    unique = datetime.now(timezone.utc).strftime("%H%M%S%f")

    user = User(auth0_sub=f"test|engine-{unique}", email=f"e{unique}@kursi.io")
    session.add(user)
    session.flush()

    org = em.Organization(name="Test Org", slug=f"test-org-{unique}", type="business")
    session.add(org)
    session.flush()

    venue = em.Venue(organization_id=org.id, name="Test Hall")
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
        layout_data={"seats": [{"uid": "A-12"}], "objects": [], "categories": []},
    )
    session.add(version)
    session.flush()

    event = em.EngineEvent(organization_id=org.id, venue_id=venue.id, title="Test Event")
    session.add(event)
    session.flush()

    perf = em.Performance(
        event_id=event.id,
        layout_version_id=version.id,
        starts_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(perf)
    session.flush()

    seat = em.PerformanceSeat(
        performance_id=perf.id, seat_uid="A-12", label="A-12", category_key="vip"
    )
    session.add(seat)
    session.flush()

    order = em.Order(
        organization_id=org.id,
        performance_id=perf.id,
        channel="marketplace",
        status="draft",
        currency="KWD",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=8),
    )
    session.add(order)
    session.flush()

    return {
        "user_id": user.id,
        "org_id": org.id,
        "venue_id": venue.id,
        "layout_id": layout.id,
        "version_id": version.id,
        "event_id": event.id,
        "performance_id": perf.id,
        "seat_id": seat.id,
        "order_id": order.id,
    }


# ── Migration round-trip ────────────────────────────────────────────────────
class TestMigrationRoundTrip:
    def test_head_is_phase1a_on_top_of_baseline(self, migrated_engine):
        with migrated_engine.connect() as conn:
            stamped = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert stamped == PHASE1A_REVISION

    def test_all_engine_tables_exist_and_legacy_untouched(self, migrated_engine):
        tables = set(inspect(migrated_engine).get_table_names())
        missing = [t for t in em.ENGINE_TABLES if t not in tables]
        assert not missing, f"migration did not create: {missing}"
        assert len(em.ENGINE_TABLES) == 17
        # The legacy marketplace tables must still be present and unrenamed.
        for legacy in ("users", "events", "bookings", "refunds", "wishlist"):
            assert legacy in tables
        # ...and the Engine's own event table must be a DIFFERENT table.
        assert "engine_events" in tables

    def test_up_down_up_round_trip(self, tmp_path):
        """Full down-migration removes every engine table and leaves legacy intact."""
        if _IS_POSTGRES:
            pytest.skip(
                "Round-trip is exercised on the module-scoped Postgres database "
                "already; re-running it here would disturb other test modules."
            )
        url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
        cfg = _alembic_config(url)

        command.upgrade(cfg, "head")
        eng = create_engine(url)
        after_up = set(inspect(eng).get_table_names())
        assert all(t in after_up for t in em.ENGINE_TABLES)

        command.downgrade(cfg, BASELINE_REVISION)
        after_down = set(inspect(eng).get_table_names())
        assert not [t for t in em.ENGINE_TABLES if t in after_down], (
            "downgrade left engine tables behind"
        )
        for legacy in ("users", "events", "bookings", "refunds", "wishlist"):
            assert legacy in after_down, f"downgrade damaged legacy table {legacy}"

        command.upgrade(cfg, "head")
        assert all(t in set(inspect(eng).get_table_names()) for t in em.ENGINE_TABLES)
        eng.dispose()


# ── Exit test 4: double-sell backstop ───────────────────────────────────────
class TestExit4DoubleSellBackstop:
    def test_second_live_ticket_on_one_seat_is_rejected(self, session, fixture_ids):
        """INVARIANT 2: partial unique index on tickets(performance_seat_id)
        WHERE status IN ('issued','checked_in')."""
        common = dict(
            order_id=fixture_ids["order_id"],
            organization_id=fixture_ids["org_id"],
            performance_id=fixture_ids["performance_id"],
            performance_seat_id=fixture_ids["seat_id"],
            amount_paid_minor=5500,
            currency="KWD",
        )
        session.add(em.Ticket(status="issued", **common))
        session.flush()

        session.add(em.Ticket(status="issued", **common))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_checked_in_also_blocks_a_second_ticket(self, session, fixture_ids):
        """'checked_in' is inside the index predicate, so it blocks too."""
        common = dict(
            order_id=fixture_ids["order_id"],
            organization_id=fixture_ids["org_id"],
            performance_id=fixture_ids["performance_id"],
            performance_seat_id=fixture_ids["seat_id"],
            amount_paid_minor=5500,
            currency="KWD",
        )
        session.add(em.Ticket(status="checked_in", **common))
        session.flush()
        session.add(em.Ticket(status="issued", **common))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_cancelled_ticket_frees_the_seat(self, session, fixture_ids):
        """cancelled/refunded fall outside the predicate, so the seat is resellable."""
        common = dict(
            order_id=fixture_ids["order_id"],
            organization_id=fixture_ids["org_id"],
            performance_id=fixture_ids["performance_id"],
            performance_seat_id=fixture_ids["seat_id"],
            amount_paid_minor=5500,
            currency="KWD",
        )
        session.add(em.Ticket(status="cancelled", **common))
        session.add(em.Ticket(status="refunded", **common))
        session.flush()
        # A live ticket may now be issued for the same seat.
        session.add(em.Ticket(status="issued", **common))
        session.flush()


# ── Exit test 5: layout immutability ────────────────────────────────────────
class TestExit5LayoutImmutability:
    def test_draft_layout_data_is_editable(self, session, fixture_ids):
        version = session.get(em.LayoutVersion, fixture_ids["version_id"])
        assert version.status == "draft"
        version.layout_data = {"seats": [{"uid": "A-12"}, {"uid": "A-13"}]}
        session.flush()  # must not raise

    def test_freezing_transition_is_allowed(self, session, fixture_ids):
        """draft -> frozen is the one legal status move."""
        version = session.get(em.LayoutVersion, fixture_ids["version_id"])
        version.status = "frozen"
        version.frozen_at = datetime.now(timezone.utc)
        session.flush()
        session.expire_all()
        assert session.get(em.LayoutVersion, fixture_ids["version_id"]).status == "frozen"

    def test_update_of_frozen_layout_data_is_rejected_by_the_database(
        self, session, fixture_ids
    ):
        """INVARIANT 3, enforced by trigger - not by application code."""
        version = session.get(em.LayoutVersion, fixture_ids["version_id"])
        version.status = "frozen"
        version.frozen_at = datetime.now(timezone.utc)
        session.flush()

        version.layout_data = {"seats": [{"uid": "TAMPERED"}]}
        with pytest.raises(DBAPIError) as exc:
            session.flush()
        assert "immutable" in str(exc.value).lower()

    def test_unfreezing_is_rejected(self, session, fixture_ids):
        """frozen -> draft must be impossible; freezing is one-way."""
        version = session.get(em.LayoutVersion, fixture_ids["version_id"])
        version.status = "frozen"
        version.frozen_at = datetime.now(timezone.utc)
        session.flush()

        version.status = "draft"
        with pytest.raises(DBAPIError) as exc:
            session.flush()
        assert "frozen" in str(exc.value).lower()

    def test_editing_a_frozen_layout_means_creating_the_next_version(
        self, session, fixture_ids
    ):
        """The prescribed escape hatch: a new draft version, not an in-place edit."""
        version = session.get(em.LayoutVersion, fixture_ids["version_id"])
        version.status = "frozen"
        version.frozen_at = datetime.now(timezone.utc)
        session.flush()

        v2 = em.LayoutVersion(
            venue_layout_id=fixture_ids["layout_id"],
            version_number=2,
            status="draft",
            created_by_user_id=fixture_ids["user_id"],
            layout_data={"seats": [{"uid": "A-12"}, {"uid": "A-13"}]},
        )
        session.add(v2)
        session.flush()
        assert v2.id != fixture_ids["version_id"]
        # v1 is untouched and still frozen - the live performance keeps reading it.
        assert session.get(em.LayoutVersion, fixture_ids["version_id"]).status == "frozen"

    def test_duplicate_version_number_per_layout_is_rejected(self, session, fixture_ids):
        session.add(
            em.LayoutVersion(
                venue_layout_id=fixture_ids["layout_id"],
                version_number=1,  # already exists
                status="draft",
                created_by_user_id=fixture_ids["user_id"],
                layout_data={},
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


# ── Lock uniqueness ─────────────────────────────────────────────────────────
class TestSeatLockUniqueness:
    def test_second_unreleased_lock_on_one_seat_is_rejected(self, session, fixture_ids):
        """INVARIANT 1: the race arbiter. Two lock attempts, one winner."""
        session.add(
            em.SeatLock(
                order_id=fixture_ids["order_id"],
                performance_seat_id=fixture_ids["seat_id"],
            )
        )
        session.flush()

        session.add(
            em.SeatLock(
                order_id=fixture_ids["order_id"],
                performance_seat_id=fixture_ids["seat_id"],
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_new_lock_succeeds_once_the_previous_one_is_released(
        self, session, fixture_ids
    ):
        first = em.SeatLock(
            order_id=fixture_ids["order_id"],
            performance_seat_id=fixture_ids["seat_id"],
        )
        session.add(first)
        session.flush()

        first.released_at = datetime.now(timezone.utc)
        session.flush()

        session.add(
            em.SeatLock(
                order_id=fixture_ids["order_id"],
                performance_seat_id=fixture_ids["seat_id"],
            )
        )
        session.flush()  # must not raise

    def test_released_locks_do_not_collide_with_each_other(self, session, fixture_ids):
        """Many released locks per seat are fine - the index only sees NULLs."""
        released = datetime.now(timezone.utc)
        for _ in range(3):
            session.add(
                em.SeatLock(
                    order_id=fixture_ids["order_id"],
                    performance_seat_id=fixture_ids["seat_id"],
                    released_at=released,
                )
            )
        session.flush()


# ── Other spec invariants ───────────────────────────────────────────────────
class TestOtherInvariants:
    def test_usage_event_is_unique_per_ticket(self, session, fixture_ids):
        ticket = em.Ticket(
            order_id=fixture_ids["order_id"],
            organization_id=fixture_ids["org_id"],
            performance_id=fixture_ids["performance_id"],
            performance_seat_id=fixture_ids["seat_id"],
            status="issued",
            amount_paid_minor=5500,
            currency="KWD",
        )
        session.add(ticket)
        session.flush()

        session.add(em.UsageEvent(organization_id=fixture_ids["org_id"], ticket_id=ticket.id))
        session.flush()
        session.add(em.UsageEvent(organization_id=fixture_ids["org_id"], ticket_id=ticket.id))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_membership_is_unique_per_org_and_user(self, session, fixture_ids):
        for _ in range(2):
            session.add(
                em.Membership(
                    organization_id=fixture_ids["org_id"],
                    user_id=fixture_ids["user_id"],
                    role="owner",
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_performance_seat_uid_is_unique_per_performance(self, session, fixture_ids):
        session.add(
            em.PerformanceSeat(
                performance_id=fixture_ids["performance_id"], seat_uid="A-12"
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_external_ref_is_unique_per_org_but_nullable_many_times(
        self, session, fixture_ids
    ):
        """Partial unique index: many NULLs allowed, duplicates rejected."""
        base = dict(
            organization_id=fixture_ids["org_id"],
            performance_id=fixture_ids["performance_id"],
            channel="api",
            currency="KWD",
        )
        # Several orders with no external_ref coexist happily.
        for _ in range(3):
            session.add(em.Order(external_ref=None, **base))
        session.flush()

        session.add(em.Order(external_ref="idem-1", **base))
        session.flush()
        session.add(em.Order(external_ref="idem-1", **base))
        with pytest.raises(IntegrityError):
            session.flush()

    @pytest.mark.parametrize(
        "model,column,bad_value",
        [
            (em.Organization, "status", "exploded"),
            (em.Organization, "type", "government"),
            (em.Organization, "plan", "unlimited"),
        ],
    )
    def test_enum_check_constraints_reject_unknown_states(
        self, session, model, column, bad_value
    ):
        unique = datetime.now(timezone.utc).strftime("%H%M%S%f")
        kwargs = {"name": "X", "slug": f"chk-{column}-{unique}", column: bad_value}
        session.add(model(**kwargs))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_ticket_status_check_rejects_unknown_state(self, session, fixture_ids):
        session.add(
            em.Ticket(
                order_id=fixture_ids["order_id"],
                organization_id=fixture_ids["org_id"],
                performance_id=fixture_ids["performance_id"],
                performance_seat_id=fixture_ids["seat_id"],
                status="teleported",
                amount_paid_minor=1,
                currency="KWD",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_performance_seat_has_no_sold_or_locked_status(self):
        """Spec 3: sold/locked are derived, never stored, to prevent status drift."""
        assert "sold" not in em.SEAT_STATUSES
        assert "locked" not in em.SEAT_STATUSES
        assert em.SEAT_STATUSES == (
            "available",
            "blocked",
            "invitation",
            "reserved_internal",
        )


# ── Exit test 6: money shape ────────────────────────────────────────────────
MONEY_COLUMNS = [
    ("engine_performance_categories", "amount_minor"),
    ("engine_performance_seats", "price_override_minor"),
    ("engine_orders", "subtotal_minor"),
    ("engine_orders", "fees_minor"),
    ("engine_orders", "discount_minor"),
    ("engine_orders", "total_minor"),
    ("engine_tickets", "amount_paid_minor"),
]


class TestExit6MoneyShape:
    @pytest.mark.parametrize("table,column", MONEY_COLUMNS)
    def test_every_monetary_column_is_an_integer_type_in_the_database(
        self, migrated_engine, table, column
    ):
        cols = {c["name"]: c for c in inspect(migrated_engine).get_columns(table)}
        assert column in cols, f"{table}.{column} missing"
        col_type = cols[column]["type"]
        assert isinstance(col_type, (BigInteger, Integer)), (
            f"{table}.{column} is {col_type!r}, not an integer type - money must "
            f"never be stored as float/numeric"
        )

    @pytest.mark.parametrize("bad", [55.0, 5.5, 0.1, Decimal("5.500")])
    def test_float_and_decimal_money_is_rejected_not_coerced(
        self, session, fixture_ids, bad
    ):
        """The safe behaviour is REJECTION.

        A bare BIGINT column would not give this: PostgreSQL rounds 100.5 to 101 and
        SQLite stores it verbatim. `MinorAmount` refuses the value at bind time, on
        every dialect, before the driver sees it.
        """
        session.add(
            em.Ticket(
                order_id=fixture_ids["order_id"],
                organization_id=fixture_ids["org_id"],
                performance_id=fixture_ids["performance_id"],
                performance_seat_id=fixture_ids["seat_id"],
                status="issued",
                amount_paid_minor=bad,
                currency="KWD",
            )
        )
        with pytest.raises((TypeError, StatementError)) as exc:
            session.flush()
        assert "minor units" in str(exc.value)

    def test_bool_is_not_a_valid_money_amount(self, session, fixture_ids):
        """bool subclasses int; it must still be refused."""
        session.add(
            em.Ticket(
                order_id=fixture_ids["order_id"],
                organization_id=fixture_ids["org_id"],
                performance_id=fixture_ids["performance_id"],
                performance_seat_id=fixture_ids["seat_id"],
                status="issued",
                amount_paid_minor=True,
                currency="KWD",
            )
        )
        with pytest.raises((TypeError, StatementError)):
            session.flush()

    def test_integer_minor_units_round_trip_exactly(self, session, fixture_ids):
        """KWD 5.500 is 5500 fils and comes back as exactly that."""
        ticket = em.Ticket(
            order_id=fixture_ids["order_id"],
            organization_id=fixture_ids["org_id"],
            performance_id=fixture_ids["performance_id"],
            performance_seat_id=fixture_ids["seat_id"],
            status="issued",
            amount_paid_minor=5500,
            currency="KWD",
        )
        session.add(ticket)
        session.flush()
        session.expire_all()

        stored = session.get(em.Ticket, ticket.id)
        assert stored.amount_paid_minor == 5500
        assert isinstance(stored.amount_paid_minor, int)
        assert stored.currency == "KWD"

    def test_negative_money_is_rejected_by_check_constraint(self, session, fixture_ids):
        session.add(
            em.Ticket(
                order_id=fixture_ids["order_id"],
                organization_id=fixture_ids["org_id"],
                performance_id=fixture_ids["performance_id"],
                performance_seat_id=fixture_ids["seat_id"],
                status="issued",
                amount_paid_minor=-1,
                currency="KWD",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_currency_must_be_three_characters(self, session, fixture_ids):
        session.add(
            em.Ticket(
                order_id=fixture_ids["order_id"],
                organization_id=fixture_ids["org_id"],
                performance_id=fixture_ids["performance_id"],
                performance_seat_id=fixture_ids["seat_id"],
                status="issued",
                amount_paid_minor=100,
                currency="KUWAIT",
            )
        )
        # The row is refused on both backends, but not by the same mechanism, so
        # the exception CLASS differs. SQLite's CHAR(3) is advisory, so the
        # `length(currency) = 3` CHECK is what fires (IntegrityError). PostgreSQL
        # enforces CHAR(3) as a real width and rejects the value on the way in,
        # before any constraint is evaluated (DataError /
        # StringDataRightTruncation). Either way an over-long currency code
        # cannot be stored, which is the invariant.
        with pytest.raises((IntegrityError, DataError)):
            session.flush()


# ── PostgreSQL-only semantics ───────────────────────────────────────────────
@postgres_only
class TestPostgresSpecificDDL:
    """The pieces that genuinely cannot be expressed on SQLite."""

    def test_freeze_guard_is_a_plpgsql_trigger(self, migrated_engine):
        with migrated_engine.connect() as conn:
            fn = conn.execute(
                text(
                    "SELECT 1 FROM pg_proc WHERE proname = "
                    "'engine_layout_versions_freeze_guard'"
                )
            ).scalar()
            trg = conn.execute(
                text(
                    "SELECT 1 FROM pg_trigger WHERE tgname = "
                    "'trg_engine_layout_versions_freeze_guard'"
                )
            ).scalar()
        assert fn == 1, "plpgsql freeze-guard function missing"
        assert trg == 1, "freeze-guard trigger missing"

    def test_partial_unique_indexes_carry_their_predicates(self, migrated_engine):
        with migrated_engine.connect() as conn:
            rows = dict(
                conn.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                         "('uq_engine_seat_locks_active_seat', 'uq_engine_tickets_live_seat')")
                ).all()
            )
        assert "WHERE (released_at IS NULL)" in rows["uq_engine_seat_locks_active_seat"]
        assert "issued" in rows["uq_engine_tickets_live_seat"]
        for definition in rows.values():
            assert "UNIQUE INDEX" in definition

    def test_scopes_is_a_real_text_array(self, migrated_engine):
        with migrated_engine.connect() as conn:
            udt = conn.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'engine_api_keys' AND column_name = 'scopes'"
                )
            ).scalar()
        assert udt == "_text", f"expected TEXT[], got {udt}"

    def test_money_columns_are_bigint_not_numeric(self, migrated_engine):
        with migrated_engine.connect() as conn:
            for table, column in MONEY_COLUMNS:
                dtype = conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": table, "c": column},
                ).scalar()
                assert dtype == "bigint", f"{table}.{column} is {dtype}"
