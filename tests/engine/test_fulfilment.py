"""Phase 1b: order completion -> tickets, and the ticket lifecycle (spec 5/6)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import engine_models as em
from app.engine_services import (
    EngineConflict,
    InvalidTicketTransition,
    OrderNotLive,
    SeatsUnavailable,
    ValidationError,
    as_utc,
    cancel_ticket,
    complete_order,
    create_draft_order,
    is_seat_available,
    order_tickets,
    refund_ticket,
    verify_credential,
)
from app.engine_services.audit import (
    ACTION_ORDER_COMPLETED,
    ACTION_TICKET_CANCELLED,
    ACTION_TICKET_ISSUED,
    ACTION_TICKET_REFUNDED,
)
from app.engine_services.errors import REASON_SOLD

from tests.engine.layouts import T0

VIP_PRICE = 25_000
STANDARD_PRICE = 12_000


def hold(session, world, seat_uids, **kwargs):
    return create_draft_order(
        session, world["org_id"], world["performance_id"], list(seat_uids),
        kwargs.pop("channel", "marketplace"), **kwargs,
    )


def seat_id_for(session, world, seat_uid):
    return session.execute(
        select(em.PerformanceSeat.id).where(
            em.PerformanceSeat.performance_id == world["performance_id"],
            em.PerformanceSeat.seat_uid == seat_uid,
        )
    ).scalar_one()


def count(session, model):
    return session.execute(select(func.count()).select_from(model)).scalar_one()


class TestCompleteOrder:
    def test_one_ticket_per_lock(self, session, published):
        order = hold(session, published, ["A-1", "A-2", "D-1"])
        result = complete_order(session, order, actor_user_id=published["user_id"])

        assert len(result.ticket_ids) == 3
        tickets = order_tickets(session, order)
        assert [t.status for t in tickets] == ["issued"] * 3
        assert {t.performance_id for t in tickets} == {published["performance_id"]}
        assert {t.organization_id for t in tickets} == {published["org_id"]}

    def test_the_order_becomes_completed(self, session, published):
        order = hold(session, published, ["A-1"])
        complete_order(session, order)
        session.expire_all()
        assert session.get(em.Order, order.id).status == "completed"

    def test_tickets_are_priced_like_the_order(self, session, published):
        order = hold(session, published, ["A-1", "D-1"])
        total = order.total_minor
        result = complete_order(session, order)

        tickets = order_tickets(session, order)
        assert sorted(t.amount_paid_minor for t in tickets) == [
            STANDARD_PRICE,
            VIP_PRICE,
        ]
        assert sum(t.amount_paid_minor for t in tickets) == total == result.total_minor
        assert {t.currency for t in tickets} == {"KWD"}

    def test_a_price_override_beats_the_category(self, session, published):
        session.execute(
            em.PerformanceSeat.__table__.update()
            .where(
                em.PerformanceSeat.__table__.c.id
                == seat_id_for(session, published, "A-1")
            )
            .values(price_override_minor=1_000, currency="KWD")
        )
        session.commit()

        order = hold(session, published, ["A-1"])
        assert order.total_minor == 1_000
        complete_order(session, order)
        assert order_tickets(session, order)[0].amount_paid_minor == 1_000

    def test_the_locks_are_released_and_the_tickets_take_over(self, session, published):
        order = hold(session, published, ["A-1"])
        seat_id = seat_id_for(session, published, "A-1")

        result = complete_order(session, order)

        assert result.locks_released == 1
        locks = list(
            session.execute(
                select(em.SeatLock).where(em.SeatLock.order_id == order.id)
            ).scalars()
        )
        assert all(lock.released_at is not None for lock in locks)
        # ...and the seat is still not sellable, now because of the ticket.
        assert not is_seat_available(session, seat_id, T0)

    def test_the_seat_now_reads_as_sold_not_locked(self, session, published):
        first = hold(session, published, ["A-1"])
        complete_order(session, first)

        with pytest.raises(SeatsUnavailable) as exc:
            hold(session, published, ["A-1"])
        assert exc.value.conflicts[0].reason == REASON_SOLD

    def test_one_usage_event_per_ticket(self, session, published):
        order = hold(session, published, ["A-1", "A-2"])
        result = complete_order(session, order)

        assert len(result.usage_event_ids) == 2
        assert count(session, em.UsageEvent) == 2
        rows = list(session.execute(select(em.UsageEvent)).scalars())
        assert {r.ticket_id for r in rows} == set(result.ticket_ids)
        assert {r.organization_id for r in rows} == {published["org_id"]}
        assert {as_utc(r.occurred_at) for r in rows} == {T0}

    def test_completing_twice_is_rejected(self, session, published):
        order = hold(session, published, ["A-1"])
        complete_order(session, order)

        with pytest.raises(OrderNotLive) as exc:
            complete_order(session, order)
        assert exc.value.detail["status"] == "completed"
        assert exc.value.detail["expired"] is False
        assert count(session, em.Ticket) == 1
        assert count(session, em.UsageEvent) == 1

    def test_a_cancelled_order_cannot_be_completed(self, session, published):
        from app.engine_services import release_order

        order = hold(session, published, ["A-1"])
        release_order(session, order)
        with pytest.raises(OrderNotLive):
            complete_order(session, order)

    def test_an_order_holding_nothing_cannot_be_completed(self, session, published):
        order = em.Order(
            organization_id=published["org_id"],
            performance_id=published["performance_id"],
            channel="api",
            status="draft",
            currency="KWD",
            expires_at=T0 + timedelta(minutes=8),
        )
        session.add(order)
        session.commit()

        with pytest.raises(ValidationError):
            complete_order(session, order)

    def test_a_seat_blocked_mid_hold_stops_the_completion(self, session, published):
        """And leaves the order exactly as it was - the transition rolls back
        with everything else."""
        order = hold(session, published, ["A-1", "A-2"])
        session.execute(
            em.PerformanceSeat.__table__.update()
            .where(
                em.PerformanceSeat.__table__.c.id
                == seat_id_for(session, published, "A-2")
            )
            .values(status="reserved_internal")
        )
        session.commit()

        with pytest.raises(SeatsUnavailable) as exc:
            complete_order(session, order)
        assert exc.value.uids() == ["A-2"]

        session.expire_all()
        assert session.get(em.Order, order.id).status == "draft"
        assert count(session, em.Ticket) == 0
        assert count(session, em.UsageEvent) == 0

    def test_completion_is_audited_per_ticket_and_per_order(self, session, published):
        order = hold(session, published, ["A-1", "A-2"])
        result = complete_order(session, order, actor_user_id=published["user_id"])

        issued = list(
            session.execute(
                select(em.AuditLog).where(em.AuditLog.action == ACTION_TICKET_ISSUED)
            ).scalars()
        )
        assert {row.entity_id for row in issued} == set(result.ticket_ids)

        completed = session.execute(
            select(em.AuditLog).where(em.AuditLog.action == ACTION_ORDER_COMPLETED)
        ).scalar_one()
        assert completed.entity_id == order.id
        assert completed.data["locks_released"] == 2
        assert completed.data["total_minor"] == VIP_PRICE * 2


class TestCredentials:
    def test_every_ticket_gets_version_1_and_a_stored_hash(self, session, published):
        order = hold(session, published, ["A-1"])
        complete_order(session, order)

        ticket = order_tickets(session, order)[0]
        assert ticket.credential_version == 1
        assert ticket.credential_hash and len(ticket.credential_hash) == 64

    def test_the_token_resolves_to_the_ticket_and_version(self, session, published):
        order = hold(session, published, ["A-1"])
        result = complete_order(session, order)

        ticket_id, token = next(iter(result.credentials.items()))
        assert verify_credential(token) == (ticket_id, 1)

    def test_the_token_itself_is_never_stored(self, session, published):
        """Only the hash is persisted, so a database leak mints no tickets."""
        order = hold(session, published, ["A-1"])
        result = complete_order(session, order)

        ticket = order_tickets(session, order)[0]
        token = result.credentials[ticket.id]
        assert token != ticket.credential_hash
        assert token not in ticket.credential_hash

    def test_a_forged_token_does_not_verify(self, session, published):
        order = hold(session, published, ["A-1"])
        result = complete_order(session, order)
        token = next(iter(result.credentials.values()))

        prefix, payload, signature = token.split(".", 2)
        assert verify_credential(f"{prefix}.{payload}.{'A' * len(signature)}") is None
        assert verify_credential("not-a-token") is None

    def test_the_token_carries_no_readable_business_data(self, session, published):
        """The seat, the price and the order are not in the QR - a scanner has
        to resolve the ticket through the API to learn any of them. (The ticket
        id and version ARE in the payload, base64-encoded and signed; that is
        what the token is FOR.)"""
        import base64

        order = hold(session, published, ["A-1"])
        result = complete_order(session, order)
        ticket_id, token = next(iter(result.credentials.items()))

        for leak in ("A-1", "KWD", "25000"):
            assert leak not in token

        # The payload decodes to exactly "<ticket_id>.<version>" and nothing else.
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        assert decoded.decode() == f"{ticket_id}.1"


class TestTicketLifecycle:
    def _issued_ticket(self, session, published, seat_uid="A-1"):
        order = hold(session, published, [seat_uid])
        complete_order(session, order)
        return order_tickets(session, order)[0]

    def test_cancelling_returns_the_seat_to_sale(self, session, published):
        ticket = self._issued_ticket(session, published)
        seat_id = ticket.performance_seat_id

        cancelled = cancel_ticket(session, ticket, reason="duplicate purchase")

        assert cancelled.status == "cancelled"
        assert is_seat_available(session, seat_id, T0)

    def test_refunding_returns_the_seat_to_sale(self, session, published):
        ticket = self._issued_ticket(session, published)
        refunded = refund_ticket(session, ticket, reason="event rescheduled")
        assert refunded.status == "refunded"
        assert is_seat_available(session, refunded.performance_seat_id, T0)

    def test_a_returned_seat_can_be_sold_again(self, session, published):
        ticket = self._issued_ticket(session, published)
        cancel_ticket(session, ticket)

        second = hold(session, published, ["A-1"])
        complete_order(session, second)
        assert count(session, em.Ticket) == 2

    def test_usage_survives_cancellation(self, session, published):
        """Decision 4: usage is monotonic. The seat comes back; the quota does
        not."""
        ticket = self._issued_ticket(session, published)
        usage_before = count(session, em.UsageEvent)

        cancel_ticket(session, ticket)

        assert count(session, em.UsageEvent) == usage_before == 1
        assert (
            session.execute(
                select(em.UsageEvent).where(em.UsageEvent.ticket_id == ticket.id)
            ).scalar_one()
            is not None
        )

    def test_usage_survives_a_refund(self, session, published):
        ticket = self._issued_ticket(session, published)
        refund_ticket(session, ticket)
        assert count(session, em.UsageEvent) == 1

    def test_reselling_a_returned_seat_bills_again(self, session, published):
        """Two issues means two usage rows - one per ticket, never merged."""
        first = self._issued_ticket(session, published)
        cancel_ticket(session, first)
        second_order = hold(session, published, ["A-1"])
        complete_order(session, second_order)

        assert count(session, em.UsageEvent) == 2

    def test_the_ticket_id_and_credential_are_untouched_by_a_cancellation(
        self, session, published
    ):
        ticket = self._issued_ticket(session, published)
        ticket_id, credential = ticket.id, ticket.credential_hash

        cancelled = cancel_ticket(session, ticket)

        assert cancelled.id == ticket_id
        assert cancelled.credential_hash == credential
        assert cancelled.credential_version == 1

    @pytest.mark.parametrize("first,second", [("cancel", "cancel"), ("cancel", "refund"),
                                              ("refund", "refund"), ("refund", "cancel")])
    def test_terminal_states_accept_nothing_further(self, session, published, first, second):
        ticket = self._issued_ticket(session, published)
        {"cancel": cancel_ticket, "refund": refund_ticket}[first](session, ticket)

        with pytest.raises(InvalidTicketTransition) as exc:
            {"cancel": cancel_ticket, "refund": refund_ticket}[second](session, ticket)
        assert exc.value.http_status == 409
        assert "terminal" in str(exc.value)

    def test_a_checked_in_ticket_may_be_refunded_but_not_cancelled(
        self, session, published
    ):
        ticket = self._issued_ticket(session, published)
        session.execute(
            em.Ticket.__table__.update()
            .where(em.Ticket.__table__.c.id == ticket.id)
            .values(status="checked_in")
        )
        session.commit()

        with pytest.raises(InvalidTicketTransition):
            cancel_ticket(session, ticket)
        assert refund_ticket(session, ticket).status == "refunded"

    def test_the_transitions_are_audited(self, session, published):
        ticket = self._issued_ticket(session, published)
        cancel_ticket(session, ticket, reason="oops")

        row = session.execute(
            select(em.AuditLog).where(em.AuditLog.action == ACTION_TICKET_CANCELLED)
        ).scalar_one()
        assert row.entity_id == ticket.id
        assert (row.data["from"], row.data["to"]) == ("issued", "cancelled")
        assert row.data["usage_event_retained"] is True

        refunded = self._issued_ticket(session, published, seat_uid="A-2")
        refund_ticket(session, refunded)
        assert (
            session.execute(
                select(func.count())
                .select_from(em.AuditLog)
                .where(em.AuditLog.action == ACTION_TICKET_REFUNDED)
            ).scalar_one()
            == 1
        )


class TestDoubleSellBackstop:
    def test_the_backstop_surfaces_as_a_named_conflict(self, session, published):
        """Force the state the lock discipline is supposed to make impossible:
        an order holding a seat that a stray ticket already occupies, with the
        availability check bypassed. The partial unique index must still stop
        it, and the service must translate that rather than leaking a driver
        error."""
        import app.engine_services.fulfilment as fulfilment

        order = hold(session, published, ["A-1"])
        seat_id = seat_id_for(session, published, "A-1")
        session.add(
            em.Ticket(
                order_id=order.id,
                organization_id=published["org_id"],
                performance_id=published["performance_id"],
                performance_seat_id=seat_id,
                status="issued",
                amount_paid_minor=1,
                currency="KWD",
            )
        )
        session.commit()

        original = fulfilment.describe_unavailable
        fulfilment.describe_unavailable = lambda *a, **k: []
        try:
            with pytest.raises(EngineConflict) as exc:
                complete_order(session, order)
        finally:
            fulfilment.describe_unavailable = original

        assert "double-sell backstop" in str(exc.value)
        session.expire_all()
        assert session.get(em.Order, order.id).status == "draft"
