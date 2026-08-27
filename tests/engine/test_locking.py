"""Phase 1b: the locking engine (spec 4, Decision 3).

Exit tests 1 (RACE), 2 (EXPIRY) and 3 (EXTENSION) live here, alongside the
all-or-nothing contract, idempotency and the housekeeping GC.

WHICH BACKEND PROVES WHAT
-------------------------
Read the docstrings on `TestExit1Race` before trusting any of it. SQLite and
PostgreSQL do not have the same concurrency model, and exactly one test in this
file needs the difference; it is gated with a visible skip rather than
approximated.
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import engine_models as em
from app.engine_services import (
    DEFAULT_HOLD_MINUTES,
    EXTENSION_MINUTES,
    EngineConflict,
    ExtensionAlreadyUsed,
    NotFound,
    OrderNotLive,
    SeatsUnavailable,
    ValidationError,
    as_utc,
    available_seat_uids,
    complete_order,
    create_draft_order,
    extend_order,
    gc_expired_locks,
    is_seat_available,
    release_order,
)
from app.engine_services.audit import (
    ACTION_ORDER_CANCELLED,
    ACTION_ORDER_CREATED,
    ACTION_ORDER_EXPIRED,
    ACTION_ORDER_EXTENDED,
)
from app.engine_services.errors import (
    REASON_LOCKED,
    REASON_SEAT_STATUS,
    REASON_SOLD,
    REASON_UNKNOWN_SEAT,
)

from tests.engine.conftest import postgres_only
from tests.engine.layouts import T0

VIP_PRICE = 25_000
STANDARD_PRICE = 12_000


# ── helpers ─────────────────────────────────────────────────────────────────
def hold(session, world, seat_uids, **kwargs):
    return create_draft_order(
        session,
        world["org_id"],
        world["performance_id"],
        list(seat_uids),
        kwargs.pop("channel", "marketplace"),
        **kwargs,
    )


def seat_id_for(session, world, seat_uid):
    return session.execute(
        select(em.PerformanceSeat.id).where(
            em.PerformanceSeat.performance_id == world["performance_id"],
            em.PerformanceSeat.seat_uid == seat_uid,
        )
    ).scalar_one()


def active_locks(session, order_id=None):
    stmt = select(em.SeatLock).where(em.SeatLock.released_at.is_(None))
    if order_id is not None:
        stmt = stmt.where(em.SeatLock.order_id == order_id)
    return list(session.execute(stmt.order_by(em.SeatLock.id)).scalars())


def count(session, model):
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ── Creating a hold ─────────────────────────────────────────────────────────
class TestCreateDraftOrder:
    def test_a_hold_is_a_draft_order_plus_one_lock_per_seat(self, session, published):
        order = hold(session, published, ["A-1", "A-2"])

        assert order.status == "draft"
        assert order.channel == "marketplace"
        assert as_utc(order.expires_at) == T0 + timedelta(minutes=DEFAULT_HOLD_MINUTES)
        assert order.extended is False

        locks = active_locks(session, order.id)
        assert len(locks) == 2
        assert all(lock.released_at is None for lock in locks)

    def test_the_hold_is_priced_from_the_performance_categories(self, session, published):
        order = hold(session, published, ["A-1", "A-2", "D-1"])
        # two VIP + one standard, in fils
        assert order.subtotal_minor == VIP_PRICE * 2 + STANDARD_PRICE
        assert order.total_minor == order.subtotal_minor
        assert order.currency == "KWD"
        assert isinstance(order.total_minor, int)

    def test_the_seats_stop_being_available_immediately(self, session, published):
        hold(session, published, ["A-1"])
        assert not is_seat_available(session, seat_id_for(session, published, "A-1"), T0)

    def test_asking_for_the_same_seat_twice_is_one_seat(self, session, published):
        order = hold(session, published, ["A-1", "A-1", "A-1"])
        assert len(active_locks(session, order.id)) == 1
        assert order.total_minor == VIP_PRICE

    def test_a_hold_can_be_placed_on_every_sellable_seat(self, session, published):
        """142 of 144 - the layout blocks two."""
        sellable = available_seat_uids(session, published["performance_id"], T0)
        assert len(sellable) == 142
        order = hold(session, published, sellable)
        assert len(active_locks(session, order.id)) == 142
        assert available_seat_uids(session, published["performance_id"], T0) == []

    def test_no_seats_is_rejected(self, session, published):
        with pytest.raises(ValidationError):
            hold(session, published, [])

    def test_an_unknown_channel_is_rejected(self, session, published):
        with pytest.raises(ValidationError):
            hold(session, published, ["A-1"], channel="carrier-pigeon")

    def test_a_performance_from_another_organization_is_not_visible(
        self, session, published
    ):
        other = em.Organization(name="Someone Else", slug="someone-else")
        session.add(other)
        session.commit()

        with pytest.raises(NotFound):
            create_draft_order(
                session, other.id, published["performance_id"], ["A-1"], "marketplace"
            )

    def test_a_draft_performance_is_not_selling(self, session, world):
        """Inventory exists but the performance was never activated."""
        from app.engine_services import publish_performance

        publish_performance(
            session, world["performance_id"], prices={"vip": VIP_PRICE,
            "standard": STANDARD_PRICE}, activate=False,
        )
        with pytest.raises(EngineConflict) as exc:
            hold(session, world, ["A-1"])
        assert exc.value.detail["status"] == "draft"

    def test_the_hold_is_audited(self, session, published):
        order = hold(session, published, ["A-1", "A-2"])
        row = session.execute(
            select(em.AuditLog).where(em.AuditLog.action == ACTION_ORDER_CREATED)
        ).scalar_one()
        assert row.entity_id == order.id
        assert row.organization_id == published["org_id"]
        assert row.data["seat_uids"] == ["A-1", "A-2"]
        assert row.data["hold_minutes"] == DEFAULT_HOLD_MINUTES


# ── All-or-nothing ──────────────────────────────────────────────────────────
class TestAllOrNothing:
    def test_one_unavailable_seat_fails_the_whole_basket(self, session, published):
        hold(session, published, ["A-4"])
        orders_before = count(session, em.Order)
        locks_before = count(session, em.SeatLock)

        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1", "A-2", "A-3", "A-4"])

        # Only the offending seat is reported - not the whole basket.
        assert exc.value.uids() == ["A-4"]
        assert exc.value.reasons() == {"A-4": REASON_LOCKED}
        # And nothing at all was left behind: no half-order, no stray locks.
        assert count(session, em.Order) == orders_before
        assert count(session, em.SeatLock) == locks_before
        assert is_seat_available(session, seat_id_for(session, published, "A-1"), T0)

    def test_the_conflict_names_the_order_holding_the_seat(self, session, published):
        first = hold(session, published, ["A-1"])
        expected_expiry = as_utc(first.expires_at).isoformat()

        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1"])

        conflict = exc.value.conflicts[0]
        assert conflict.reason == REASON_LOCKED
        assert conflict.detail["held_by_order_id"] == first.id
        assert conflict.detail["held_until"] == expected_expiry

    def test_a_blocked_seat_is_reported_as_seat_status(self, session, published):
        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1", "F-6"])
        conflict = exc.value.conflicts[0]
        assert (conflict.seat_uid, conflict.reason) == ("F-6", REASON_SEAT_STATUS)
        assert conflict.detail["status"] == "blocked"

    def test_a_sold_seat_is_reported_as_sold(self, session, published):
        sold = hold(session, published, ["A-1"])
        complete_order(session, sold)

        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1"])
        conflict = exc.value.conflicts[0]
        assert conflict.reason == REASON_SOLD
        assert conflict.detail["ticket_status"] == "issued"

    def test_an_unknown_seat_uid_is_reported_not_ignored(self, session, published):
        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1", "Z-99"])
        assert exc.value.reasons() == {"Z-99": REASON_UNKNOWN_SEAT}

    def test_every_offending_seat_is_reported_at_once(self, session, published):
        hold(session, published, ["A-2"])
        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1", "A-2", "F-6"])
        assert exc.value.reasons() == {"A-2": REASON_LOCKED, "F-6": REASON_SEAT_STATUS}

    def test_the_error_serialises_to_something_an_api_can_return(self, session, published):
        hold(session, published, ["A-1"])
        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1"])

        body = exc.value.as_dict()
        assert body["error"] == "seats_unavailable"
        assert exc.value.http_status == 409
        assert body["conflicts"][0]["seat_uid"] == "A-1"
        assert body["conflicts"][0]["reason"] == REASON_LOCKED


# ── Exit test 1: the race ───────────────────────────────────────────────────
class TestExit1Race:
    def test_the_conflict_path_is_the_integrity_error_not_a_pre_check(
        self, session, published
    ):
        """The arbiter mechanics, deterministically.

        `create_draft_order` does not ask "is this seat free" before inserting -
        it inserts and lets `uq_engine_seat_locks_active_seat` answer. The proof
        here is that the losing call got far enough to INSERT its order row and
        then had to roll the whole thing back: exactly one order survives.
        """
        winner = hold(session, published, ["A-1"])

        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1"])

        assert count(session, em.Order) == 1
        assert session.execute(select(em.Order.id)).scalar_one() == winner.id
        assert len(active_locks(session)) == 1
        assert exc.value.conflicts[0].detail["held_by_order_id"] == winner.id

    def test_two_threads_one_seat_exactly_one_wins(
        self, session, published, session_factory
    ):
        """Exit test 1, with real threads on whichever backend is configured.

        WHAT THIS PROVES EVERYWHERE: two concurrent callers, each with its own
        session and connection, released together by a barrier; exactly one ends
        up holding A-1 and the other receives the structured conflict. The
        winner is chosen by the partial unique index, not by application code.

        WHAT IT DOES *NOT* PROVE ON SQLITE: that the two write transactions
        overlapped. SQLite admits one writer at a time, and the harness makes
        them queue (BEGIN IMMEDIATE + busy_timeout) instead of failing with
        "database is locked". The loser therefore meets an already-committed
        lock rather than blocking on the index mid-transaction. The test below
        covers that case and is Postgres-gated.
        """
        # End this session's read transaction first: under the SQLite harness a
        # held transaction owns the write lock and the workers would queue on it.
        session.rollback()

        barrier = threading.Barrier(2)
        outcomes: dict[int, tuple] = {}

        def attempt(index: int) -> None:
            worker = session_factory()
            try:
                barrier.wait(timeout=15)
                order = create_draft_order(
                    worker,
                    published["org_id"],
                    published["performance_id"],
                    ["A-1"],
                    "marketplace",
                )
                outcomes[index] = ("won", order.id)
            except SeatsUnavailable as conflict:
                outcomes[index] = ("lost", conflict.reasons())
            except Exception as exc:  # pragma: no cover - reported below
                outcomes[index] = ("error", repr(exc))
            finally:
                worker.close()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        verdicts = [outcome[0] for outcome in outcomes.values()]
        assert verdicts.count("error") == 0, outcomes
        assert verdicts.count("won") == 1, outcomes
        assert verdicts.count("lost") == 1, outcomes

        loser = next(o for o in outcomes.values() if o[0] == "lost")
        assert loser[1] == {"A-1": REASON_LOCKED}

        # The database agrees: one active lock on A-1, held by the winner.
        winner_id = next(o[1] for o in outcomes.values() if o[0] == "won")
        locks = active_locks(session)
        assert len(locks) == 1
        assert locks[0].order_id == winner_id
        assert locks[0].performance_seat_id == seat_id_for(session, published, "A-1")

    @postgres_only
    def test_the_loser_blocks_on_the_index_until_the_winner_commits(
        self, session, published, session_factory
    ):
        """The genuinely interleaved race - PostgreSQL only.

        Session A inserts a lock on A-1 and holds its transaction OPEN. A second
        thread calls `create_draft_order` for the same seat. Under PostgreSQL
        the competing INSERT blocks inside the unique index until A resolves -
        it does not fail fast, and it does not succeed. When A commits, the
        index raises, and the loser turns that into the structured conflict.

        SQLite cannot express this: it has no row-level index waiting, so the
        second thread would be queuing on the database-level write lock instead,
        which would prove nothing about the arbiter. Hence the skip rather than
        a sequential approximation.
        """
        session.rollback()
        seat_id = seat_id_for(session, published, "A-1")
        session.commit()

        holder = session_factory()
        holder_order = em.Order(
            organization_id=published["org_id"],
            performance_id=published["performance_id"],
            channel="box_office",
            status="draft",
            currency="KWD",
            expires_at=T0 + timedelta(minutes=8),
        )
        holder.add(holder_order)
        holder.flush()
        holder.add(em.SeatLock(order_id=holder_order.id, performance_seat_id=seat_id))
        holder.flush()  # written, NOT committed - the transaction stays open

        finished = threading.Event()
        outcome: dict[str, object] = {}

        def contend() -> None:
            worker = session_factory()
            try:
                create_draft_order(
                    worker,
                    published["org_id"],
                    published["performance_id"],
                    ["A-1"],
                    "marketplace",
                )
                outcome["result"] = "won"
            except SeatsUnavailable as conflict:
                outcome["result"] = "lost"
                outcome["reasons"] = conflict.reasons()
            except Exception as exc:  # pragma: no cover
                outcome["result"] = f"error: {exc!r}"
            finally:
                worker.close()
                finished.set()

        thread = threading.Thread(target=contend)
        thread.start()

        # It must still be waiting: a fail-fast here would mean the index was
        # not serialising the two inserts.
        assert not finished.wait(1.0), (
            "the competing INSERT did not block on the unique index"
        )

        holder.commit()
        assert finished.wait(15), "the competing INSERT never unblocked"
        thread.join(timeout=5)
        holder.close()

        assert outcome["result"] == "lost", outcome
        assert outcome["reasons"] == {"A-1": REASON_LOCKED}
        assert len(active_locks(session)) == 1


# ── Exit test 2: expiry is timestamp truth ──────────────────────────────────
class TestExit2Expiry:
    def test_the_predicate_flips_the_microsecond_the_deadline_passes(
        self, session, published, manual_clock
    ):
        hold(session, published, ["A-1"])
        seat_id = seat_id_for(session, published, "A-1")
        deadline = T0 + timedelta(minutes=DEFAULT_HOLD_MINUTES)

        assert not is_seat_available(session, seat_id, deadline - timedelta(microseconds=1))
        # `expires_at > now` - at the deadline itself the hold is over.
        assert is_seat_available(session, seat_id, deadline)

    def test_an_expired_holds_seats_are_lockable_immediately_without_gc(
        self, session, published, manual_clock
    ):
        """Exit test 2. No sweeper runs; nothing has touched the old rows."""
        first = hold(session, published, ["A-1", "A-2"])
        old_lock_ids = [lock.id for lock in active_locks(session, first.id)]

        manual_clock.advance(timedelta(minutes=DEFAULT_HOLD_MINUTES, seconds=1))

        second = hold(session, published, ["A-1", "A-2"])
        assert second.id != first.id
        assert len(active_locks(session, second.id)) == 2

        # The expired order was never swept: still 'draft', never 'expired'.
        session.expire_all()
        assert session.get(em.Order, first.id).status == "draft"
        # gc_expired_locks was not called anywhere in this test.
        assert (
            session.execute(
                select(func.count())
                .select_from(em.AuditLog)
                .where(em.AuditLog.action == ACTION_ORDER_EXPIRED)
            ).scalar_one()
            == 0
        )
        # The dead rows were reclaimed inline by the new hold, in its own
        # transaction - which is what lets the unique index accept the new lock.
        for lock_id in old_lock_ids:
            assert session.get(em.SeatLock, lock_id).released_at is not None

    def test_an_expired_hold_cannot_be_completed(self, session, published, manual_clock):
        order = hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=DEFAULT_HOLD_MINUTES, seconds=1))

        with pytest.raises(OrderNotLive) as exc:
            complete_order(session, order)
        assert exc.value.detail["expired"] is True

    def test_an_expired_hold_cannot_be_extended(self, session, published, manual_clock):
        order = hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=DEFAULT_HOLD_MINUTES, seconds=1))

        with pytest.raises(OrderNotLive):
            extend_order(session, order)

    def test_a_seat_freed_by_expiry_can_be_sold_by_the_new_holder(
        self, session, published, manual_clock
    ):
        """The whole point: the seat goes back on sale, for real."""
        abandoned = hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=9))

        buyer = hold(session, published, ["A-1"])
        result = complete_order(session, buyer)
        assert len(result.ticket_ids) == 1
        session.expire_all()
        assert session.get(em.Order, abandoned.id).status == "draft"


# ── Exit test 3: the single extension ───────────────────────────────────────
class TestExit3Extension:
    def test_one_extension_of_exactly_four_minutes(self, session, published):
        order = hold(session, published, ["A-1"])
        extended = extend_order(session, order)

        assert extended.extended is True
        assert as_utc(extended.expires_at) == T0 + timedelta(
            minutes=DEFAULT_HOLD_MINUTES + EXTENSION_MINUTES
        )

    def test_the_second_attempt_is_a_structured_rejection(self, session, published):
        order = hold(session, published, ["A-1"])
        extend_order(session, order)

        with pytest.raises(ExtensionAlreadyUsed) as exc:
            extend_order(session, order)

        assert exc.value.code == "extension_already_used"
        assert exc.value.http_status == 409
        assert exc.value.detail["order_id"] == order.id
        # ...and it did not quietly move the deadline anyway.
        session.expire_all()
        assert as_utc(session.get(em.Order, order.id).expires_at) == T0 + timedelta(
            minutes=12
        )

    def test_all_the_orders_seats_share_the_one_expiry(self, session, published):
        """A lock has no deadline of its own - it inherits the order's, so one
        UPDATE moves all of them."""
        order = hold(session, published, ["A-1", "A-2", "B-5", "D-12"])
        seat_ids = [
            seat_id_for(session, published, uid) for uid in ("A-1", "A-2", "B-5", "D-12")
        ]
        extend_order(session, order)

        just_before = T0 + timedelta(minutes=12) - timedelta(microseconds=1)
        at_expiry = T0 + timedelta(minutes=12)
        assert all(not is_seat_available(session, s, just_before) for s in seat_ids)
        assert all(is_seat_available(session, s, at_expiry) for s in seat_ids)

    def test_a_cancelled_order_cannot_be_extended(self, session, published):
        order = hold(session, published, ["A-1"])
        release_order(session, order)
        with pytest.raises(OrderNotLive):
            extend_order(session, order)

    def test_the_extension_is_audited(self, session, published):
        order = hold(session, published, ["A-1"])
        extend_order(session, order)
        row = session.execute(
            select(em.AuditLog).where(em.AuditLog.action == ACTION_ORDER_EXTENDED)
        ).scalar_one()
        assert row.entity_id == order.id
        assert row.data["minutes"] == EXTENSION_MINUTES


# ── external_ref idempotency ────────────────────────────────────────────────
class TestExternalRefIdempotency:
    def test_the_same_key_returns_the_original_order(self, session, published):
        first = hold(session, published, ["A-1", "A-2"], external_ref="checkout-42")
        second = hold(session, published, ["A-1", "A-2"], external_ref="checkout-42")

        assert second.id == first.id
        assert count(session, em.Order) == 1
        assert len(active_locks(session, first.id)) == 2

    def test_the_same_key_with_different_seats_still_returns_the_original(
        self, session, published
    ):
        """A retried request is a retry, not a new basket - and it must not
        lock a second set of seats."""
        first = hold(session, published, ["A-1"], external_ref="checkout-42")
        again = hold(session, published, ["B-1", "B-2"], external_ref="checkout-42")

        assert again.id == first.id
        assert len(active_locks(session)) == 1
        assert is_seat_available(session, seat_id_for(session, published, "B-1"), T0)

    def test_the_key_is_scoped_to_the_organization(self, session, published):
        hold(session, published, ["A-1"], external_ref="checkout-42")

        other = em.Organization(name="Rival Promoter", slug="rival-promoter")
        session.add(other)
        session.flush()
        other_event = em.EngineEvent(organization_id=other.id, title="Rival Event")
        session.add(other_event)
        session.flush()
        other_perf = em.Performance(
            event_id=other_event.id,
            layout_version_id=published["version_id"],
            starts_at=T0 + timedelta(days=40),
            status="on_sale",
        )
        session.add(other_perf)
        session.flush()
        session.add(
            em.PerformanceSeat(
                performance_id=other_perf.id,
                seat_uid="A-1",
                category_key="vip",
                price_override_minor=5_000,
                currency="KWD",
            )
        )
        session.commit()

        theirs = create_draft_order(
            session, other.id, other_perf.id, ["A-1"], "api", "checkout-42"
        )
        assert theirs.organization_id == other.id
        assert count(session, em.Order) == 2

    def test_orders_without_a_key_never_collide(self, session, published):
        first = hold(session, published, ["A-1"])
        second = hold(session, published, ["A-2"])
        assert first.id != second.id
        assert count(session, em.Order) == 2


# ── Releasing ───────────────────────────────────────────────────────────────
class TestReleaseOrder:
    def test_cancelling_frees_the_seats_at_once(self, session, published):
        order = hold(session, published, ["A-1", "A-2"])
        cancelled = release_order(session, order, reason="customer changed mind")

        assert cancelled.status == "cancelled"
        assert active_locks(session, order.id) == []
        assert is_seat_available(session, seat_id_for(session, published, "A-1"), T0)
        # The rows are kept, marked released - locks are an audit trail too.
        assert count(session, em.SeatLock) == 2

    def test_cancelling_twice_is_a_no_op(self, session, published):
        order = hold(session, published, ["A-1"])
        release_order(session, order)
        again = release_order(session, order)
        assert again.status == "cancelled"

    def test_an_expired_hold_can_still_be_tidied_away(self, session, published, manual_clock):
        order = hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=9))
        assert release_order(session, order).status == "cancelled"

    def test_a_completed_order_refuses_to_be_cancelled(self, session, published):
        order = hold(session, published, ["A-1"])
        complete_order(session, order)
        with pytest.raises(OrderNotLive) as exc:
            release_order(session, order)
        assert exc.value.detail["status"] == "completed"

    def test_the_cancellation_is_audited(self, session, published):
        order = hold(session, published, ["A-1", "A-2"])
        release_order(session, order, reason="timeout")
        row = session.execute(
            select(em.AuditLog).where(em.AuditLog.action == ACTION_ORDER_CANCELLED)
        ).scalar_one()
        assert row.data["locks_released"] == 2
        assert row.data["reason"] == "timeout"


# ── Housekeeping ────────────────────────────────────────────────────────────
class TestGarbageCollection:
    def test_it_expires_dead_orders_and_releases_their_locks(
        self, session, published, manual_clock
    ):
        order = hold(session, published, ["A-1", "A-2"])
        manual_clock.advance(timedelta(minutes=9))

        result = gc_expired_locks(session)

        assert (result.orders_expired, result.locks_released) == (1, 2)
        session.expire_all()
        assert session.get(em.Order, order.id).status == "expired"
        assert active_locks(session, order.id) == []

    def test_it_leaves_live_holds_alone(self, session, published, manual_clock):
        order = hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=7))

        result = gc_expired_locks(session)

        assert (result.orders_expired, result.locks_released) == (0, 0)
        session.expire_all()
        assert session.get(em.Order, order.id).status == "draft"
        assert len(active_locks(session, order.id)) == 1

    def test_it_is_idempotent(self, session, published, manual_clock):
        hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=9))

        gc_expired_locks(session)
        second = gc_expired_locks(session)
        assert (second.orders_expired, second.locks_released) == (0, 0)

    def test_it_changes_nothing_about_availability(
        self, session, published, manual_clock
    ):
        """The point of the whole design: running it or not running it produces
        the same answer to "is this seat free"."""
        hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=9))
        seat_id = seat_id_for(session, published, "A-1")

        before = is_seat_available(session, seat_id, manual_clock.now())
        gc_expired_locks(session)
        after = is_seat_available(session, seat_id, manual_clock.now())

        assert before is after is True

    def test_it_can_be_scoped_to_one_organization(self, session, published, manual_clock):
        hold(session, published, ["A-1"])
        manual_clock.advance(timedelta(minutes=9))

        assert gc_expired_locks(session, organization_id=-1).orders_expired == 0
        assert (
            gc_expired_locks(session, organization_id=published["org_id"]).orders_expired
            == 1
        )

    def test_it_also_tidies_locks_left_by_cancelled_orders(self, session, published):
        """`release_order` already releases its own locks; this proves the GC
        would catch any that a crash left behind."""
        order = hold(session, published, ["A-1"])
        session.execute(
            em.Order.__table__.update()
            .where(em.Order.__table__.c.id == order.id)
            .values(status="cancelled")
        )
        session.commit()

        assert gc_expired_locks(session).locks_released == 1
