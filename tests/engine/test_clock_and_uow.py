"""Phase 1b: the two pieces of plumbing everything else stands on.

The clock, because expiry is timestamp truth and a service that quietly read
the wall clock would pass every other test in this package and still be wrong in
production. The unit of work, because a hold that is not committed holds nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import engine_models as em
from app.engine_services import (
    DEFAULT_HOLD_MINUTES,
    DatabaseClock,
    ManualClock,
    as_utc,
    create_draft_order,
    get_clock,
    using_clock,
)
from app.engine_services.clock import _CLOCK  # noqa: F401  (identity check below)
from app.engine_services.uow import NestedUnitOfWork, unit_of_work

from tests.engine.layouts import T0


class TestDatabaseClock:
    def test_it_asks_the_database_and_answers_in_utc(self, session):
        """The production clock is the DATABASE's, so every process agrees."""
        before = datetime.now(timezone.utc)
        answer = DatabaseClock().now(session)
        after = datetime.now(timezone.utc)

        assert answer.tzinfo is not None
        assert answer.utcoffset() == timedelta(0)

        if session.get_bind().dialect.name == "sqlite":
            # The SQLite harness IS this machine, so the database's answer must
            # sit between the two Python readings, give or take a second of
            # clock granularity.
            assert before - timedelta(seconds=2) <= answer <= after + timedelta(seconds=2)
        else:
            # A REMOTE PostgreSQL keeps its OWN clock, and the entire point of
            # DatabaseClock is that the engine trusts that one rather than
            # whichever machine happened to make the call - two web dynos with
            # skewed wall clocks must still agree on when a hold expires.
            # Demanding agreement with the local clock here would assert the
            # opposite of the design. What is still worth checking is that the
            # value is a plausible instant and not a timezone conversion gone
            # wrong by hours.
            assert abs(answer - before) < timedelta(hours=1), (
                "the database clock is more than an hour from this machine's; "
                "that is a real skew problem, not the expected small drift"
            )

    def test_it_has_sub_second_resolution(self, session):
        """`CURRENT_TIMESTAMP` on SQLite is whole seconds - too coarse to reason
        about an eight-minute hold near its boundary, hence `strftime(..%f)`."""
        clock = DatabaseClock()
        readings = {clock.now(session).microsecond for _ in range(5)}
        assert readings != {0}

    def test_the_default_clock_is_the_database_one(self):
        """A ManualClock must never be what production picks up. The autouse
        fixture installs one, so this checks the module's own default."""
        import importlib

        import app.engine_services.clock as clock_module

        fresh = importlib.reload(clock_module)
        try:
            assert isinstance(fresh.get_clock(), fresh.DatabaseClock)
        finally:
            importlib.reload(clock_module)


class TestManualClock:
    def test_advance_and_set(self):
        clock = ManualClock(T0)
        assert clock.now() == T0
        assert clock.advance(timedelta(minutes=3)) == T0 + timedelta(minutes=3)
        assert clock.set(T0) == T0

    def test_a_naive_instant_is_read_as_utc(self):
        naive = datetime(2026, 1, 15, 12, 0, 0)
        assert ManualClock(naive).now() == T0

    def test_using_clock_restores_the_previous_one(self, session):
        outer = get_clock()
        with using_clock(ManualClock(T0)) as inner:
            assert get_clock() is inner
        assert get_clock() is outer


class TestServicesUseTheInjectedClock:
    def test_the_hold_deadline_comes_from_the_clock_not_the_wall(
        self, session, published
    ):
        """If any service read `datetime.now()` directly, this would fail."""
        far_future = datetime(2031, 6, 1, 9, 30, tzinfo=timezone.utc)

        with using_clock(ManualClock(far_future)):
            order = create_draft_order(
                session, published["org_id"], published["performance_id"], ["A-1"],
                "marketplace",
            )

        assert as_utc(order.expires_at) == far_future + timedelta(
            minutes=DEFAULT_HOLD_MINUTES
        )

    def test_released_at_comes_from_the_clock_too(self, session, published):
        from app.engine_services import release_order

        order = create_draft_order(
            session, published["org_id"], published["performance_id"], ["A-1"],
            "marketplace",
        )
        moment = T0 + timedelta(minutes=2)
        with using_clock(ManualClock(moment)):
            release_order(session, order)

        lock = session.execute(
            select(em.SeatLock).where(em.SeatLock.order_id == order.id)
        ).scalar_one()
        assert as_utc(lock.released_at) == moment


class TestUnitOfWork:
    def test_it_commits_on_success(self, session):
        with unit_of_work(session):
            session.add(em.Organization(name="Committed", slug="committed"))
        session.rollback()  # would undo anything not already committed
        assert (
            session.execute(select(func.count()).select_from(em.Organization)).scalar_one()
            == 1
        )

    def test_it_rolls_back_on_failure(self, session):
        with pytest.raises(RuntimeError):
            with unit_of_work(session):
                session.add(em.Organization(name="Doomed", slug="doomed"))
                session.flush()
                raise RuntimeError("boom")

        assert (
            session.execute(select(func.count()).select_from(em.Organization)).scalar_one()
            == 0
        )

    def test_it_refuses_to_nest(self, session):
        """Nesting would let an inner block commit half of the outer one."""
        with unit_of_work(session):
            with pytest.raises(NestedUnitOfWork):
                with unit_of_work(session):
                    pass

    def test_the_flag_is_cleared_after_a_failure(self, session):
        with pytest.raises(RuntimeError):
            with unit_of_work(session):
                raise RuntimeError("boom")
        # A later call on the same session must still work.
        with unit_of_work(session):
            session.add(em.Organization(name="Later", slug="later"))
        assert (
            session.execute(select(func.count()).select_from(em.Organization)).scalar_one()
            == 1
        )
