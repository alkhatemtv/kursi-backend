"""The /v1 surface end to end (Phase 1c).

These run against the ALEMBIC-migrated database, not a `create_all` one, because
two of the things asserted here are enforced by the database and by nothing else:
the frozen-layout trigger behind the 409, and the partial unique indexes behind
the seat conflicts. They also run on the injected `manual_clock`, so hold expiry
is driven rather than waited for - an eight-minute hold is tested in
milliseconds, against the same code path production uses.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app import engine_models as em
from tests.engine.conftest import key_header, role_header
from tests.engine.layouts import PRICES, TOTAL_SEATS, make_layout_data

pytestmark = pytest.mark.usefixtures("api_world")


def org_url(world, suffix: str = "") -> str:
    return f"/v1/orgs/{world['org_id']}{suffix}"


def envelope(response) -> dict:
    body = response.json()
    assert "error" in body and isinstance(body["error"], str)
    assert "message" in body and isinstance(body["message"], str)
    return body


@pytest.fixture
def sql_log(db_engine):
    """Every SQL statement a block of code issues, for N+1 assertions."""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", _record)
    yield statements
    event.remove(db_engine, "before_cursor_execute", _record)


# ── Layouts and the freeze invariant ────────────────────────────────────────
class TestLayoutVersions:
    def test_creating_a_version_without_a_document_copies_the_latest(
        self, api_client, world
    ):
        response = api_client.post(
            org_url(world, f"/layouts/{world['layout_id']}/versions"),
            json={},
            headers=role_header("venue_manager"),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["version_number"] == 2
        assert body["status"] == "draft"
        assert len(body["layout_data"]["seats"]) == TOTAL_SEATS

    def test_an_unusable_document_is_rejected_while_it_is_still_a_draft(
        self, api_client, world
    ):
        response = api_client.post(
            org_url(world, f"/layouts/{world['layout_id']}/versions"),
            json={"layout_data": {"seats": [{"uid": "A-1"}, {"uid": "A-1"}]}},
            headers=role_header("venue_manager"),
        )
        assert response.status_code == 422
        body = envelope(response)
        assert body["error"] == "layout_invalid"
        assert any("duplicates seat_uid" in p for p in body["detail"]["problems"])

    def test_a_draft_document_can_be_replaced(self, api_client, world):
        smaller = make_layout_data(rows=2, cols=2, blocked=(), accessible=())
        response = api_client.put(
            org_url(world, f"/layout-versions/{world['version_id']}/layout-data"),
            json={"layout_data": smaller},
            headers=role_header("venue_manager"),
        )
        assert response.status_code == 200
        assert len(response.json()["layout_data"]["seats"]) == 4

    def test_the_database_refuses_to_edit_a_frozen_version(
        self, api_client, world, published
    ):
        """Not a check in the route - the Phase 1a trigger, surfaced as 409."""
        version = api_client.get(
            org_url(world, f"/layout-versions/{world['version_id']}"),
            headers=role_header("venue_manager"),
        ).json()
        assert version["status"] == "frozen"

        response = api_client.put(
            org_url(world, f"/layout-versions/{world['version_id']}/layout-data"),
            json={"layout_data": make_layout_data(rows=2, cols=2, blocked=(), accessible=())},
            headers=role_header("venue_manager"),
        )
        assert response.status_code == 409
        body = envelope(response)
        assert body["error"] == "layout_frozen"
        assert "never unfrozen" in body["message"] or "frozen" in body["message"]

    def test_editing_a_frozen_layout_means_the_next_version(
        self, api_client, world, published
    ):
        response = api_client.post(
            org_url(world, f"/layouts/{world['layout_id']}/versions"),
            json={"layout_data": make_layout_data(rows=3, cols=3, blocked=(), accessible=())},
            headers=role_header("venue_manager"),
        )
        assert response.status_code == 201
        assert (response.json()["version_number"], response.json()["status"]) == (2, "draft")

        # v1 is untouched; the live performance still reads it.
        still_frozen = api_client.get(
            org_url(world, f"/layout-versions/{world['version_id']}"),
            headers=role_header("venue_manager"),
        ).json()
        assert still_frozen["status"] == "frozen"
        assert len(still_frozen["layout_data"]["seats"]) == TOTAL_SEATS


# ── Publishing ──────────────────────────────────────────────────────────────
class TestPublish:
    def test_publishing_freezes_prices_and_materialises_inventory(
        self, api_client, world
    ):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/publish",
            json={"prices": PRICES},
            headers=role_header("event_manager"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["froze_layout"] is True
        assert body["seats_created"] == TOTAL_SEATS
        assert body["seats_total"] == TOTAL_SEATS
        assert body["categories_created"] == 2
        assert body["status"] == "on_sale"

    def test_publishing_again_creates_nothing_and_reprices(self, api_client, world):
        url = f"/v1/performances/{world['performance_id']}/publish"
        api_client.post(url, json={"prices": PRICES}, headers=role_header("owner"))
        again = api_client.post(
            url,
            json={"prices": {"vip": 30_000, "standard": 12_000}},
            headers=role_header("owner"),
        )
        assert again.status_code == 200
        body = again.json()
        assert (body["froze_layout"], body["seats_created"]) == (False, 0)
        assert body["seats_existing"] == TOTAL_SEATS
        assert body["categories_updated"] == 1

    def test_a_float_price_is_422_and_says_minor_units(self, api_client, world):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/publish",
            json={"prices": {"vip": 25.5, "standard": 12_000}},
            headers=role_header("owner"),
        )
        assert response.status_code == 422
        body = envelope(response)
        assert "minor units" in body["message"]
        assert "5500" in body["message"] or "->" in body["message"]

    def test_a_price_that_looks_whole_is_still_a_float_and_still_refused(
        self, api_client, world
    ):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/publish",
            json={"prices": {"vip": 25000.0, "standard": 12_000}},
            headers=role_header("owner"),
        )
        assert response.status_code == 422
        assert "minor units" in envelope(response)["message"]

    def test_an_unpriced_category_is_refused_before_anything_freezes(
        self, api_client, world, session
    ):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/publish",
            json={"prices": {"vip": 25_000}},
            headers=role_header("owner"),
        )
        assert response.status_code == 422
        assert "standard" in envelope(response)["message"]
        session.expire_all()
        assert session.get(em.LayoutVersion, world["version_id"]).status == "draft"


# ── Availability ────────────────────────────────────────────────────────────
class TestAvailability:
    def test_the_seat_map_has_the_shape_an_sdk_needs(self, api_client, published, world):
        response = api_client.get(
            f"/v1/performances/{world['performance_id']}/availability",
            headers=role_header("marketing"),
        )
        assert response.status_code == 200
        body = response.json()

        assert body["counts"]["total"] == TOTAL_SEATS
        assert body["counts"]["blocked"] == 2  # F-6, F-7 from the fixture layout
        assert body["counts"]["available"] == TOTAL_SEATS - 2
        assert body["as_of"].endswith("+00:00")

        seat = next(s for s in body["seats"] if s["uid"] == "A-1")
        assert set(seat) == {
            "uid", "id", "label", "section", "row", "number", "x", "y",
            "category", "status", "inventory_status", "accessibility",
            "amount_minor", "currency", "held_until",
        }
        assert seat["status"] == "available"
        assert seat["category"] == "vip"
        assert seat["amount_minor"] == PRICES["vip"]
        assert seat["currency"] == "KWD"
        assert seat["accessibility"] is True

        blocked = next(s for s in body["seats"] if s["uid"] == "F-6")
        assert (blocked["status"], blocked["inventory_status"]) == ("blocked", "blocked")

        assert [c["key"] for c in body["categories"]] == ["standard", "vip"]
        assert next(c for c in body["categories"] if c["key"] == "vip")[
            "amount_minor"
        ] == PRICES["vip"]

    def test_held_and_sold_seats_are_distinguished(
        self, api_client, published, world
    ):
        performance = world["performance_id"]
        held = api_client.post(
            f"/v1/performances/{performance}/orders",
            json={"seat_uids": ["B-1"], "channel": "api"},
            headers=role_header("box_office"),
        )
        assert held.status_code == 201

        sold_order = api_client.post(
            f"/v1/performances/{performance}/orders",
            json={"seat_uids": ["C-1"], "channel": "api"},
            headers=role_header("box_office"),
        ).json()
        api_client.post(
            f"/v1/orders/{sold_order['id']}/complete", headers=role_header("box_office")
        )

        seats = {
            s["uid"]: s
            for s in api_client.get(
                f"/v1/performances/{performance}/availability",
                headers=role_header("marketing"),
            ).json()["seats"]
        }
        assert seats["B-1"]["status"] == "held"
        assert seats["B-1"]["held_until"] is not None
        assert seats["C-1"]["status"] == "sold"
        assert seats["C-1"]["held_until"] is None
        assert seats["D-1"]["status"] == "available"

    def test_it_is_one_query_over_the_inventory_however_big_the_house(
        self, api_client, published, world, sql_log
    ):
        sql_log.clear()
        api_client.get(
            f"/v1/performances/{world['performance_id']}/availability",
            headers=role_header("marketing"),
        )
        seat_reads = [s for s in sql_log if "engine_performance_seats" in s]
        assert len(seat_reads) == 1, (
            f"the seat map must be one pass over inventory; got {len(seat_reads)}"
        )

    def test_an_on_sale_performance_is_readable_without_a_credential(
        self, api_client, published, world, session
    ):
        # `published` leaves the performance on_sale; the event is 'active'.
        response = api_client.get(
            f"/v1/performances/{world['performance_id']}/availability"
        )
        assert response.status_code == 200
        assert response.json()["counts"]["total"] == TOTAL_SEATS

    def test_a_draft_performance_is_not_public(self, api_client, world):
        response = api_client.get(
            f"/v1/performances/{world['performance_id']}/availability"
        )
        assert response.status_code == 401
        assert envelope(response)["error"] == "unauthenticated"

    def test_a_stranger_cannot_use_it_as_an_existence_oracle(self, api_client):
        """A performance that does not exist and one that is not public yet must
        look identical from outside."""
        missing = api_client.get("/v1/performances/999999/availability")
        assert missing.status_code == 401
        assert envelope(missing)["error"] == "unauthenticated"

    def test_a_read_scoped_key_may_read_the_map(
        self, api_client, published, world, make_api_key
    ):
        token = make_api_key(world["org_id"], scopes=["read"])
        response = api_client.get(
            f"/v1/performances/{world['performance_id']}/availability",
            headers=key_header(token),
        )
        assert response.status_code == 200


# ── Checkout ────────────────────────────────────────────────────────────────
class TestCheckout:
    def test_the_happy_path(self, api_client, published, world, manual_clock):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/orders",
            json={"seat_uids": ["A-1", "A-2"], "channel": "api",
                  "external_ref": "cart-1", "customer_email": "buyer@example.com"},
            headers=role_header("box_office"),
        )
        assert response.status_code == 201
        order = response.json()
        assert order["status"] == "draft"
        assert order["total_minor"] == 2 * PRICES["vip"]
        assert order["currency"] == "KWD"
        assert order["extended"] is False
        assert order["expires_at"] == (manual_clock.now() + timedelta(minutes=8)).isoformat()

        detail = api_client.get(
            f"/v1/orders/{order['id']}", headers=role_header("box_office")
        ).json()
        assert detail["seat_uids"] == ["A-1", "A-2"]

    def test_the_external_ref_is_an_idempotency_key(
        self, api_client, published, world
    ):
        url = f"/v1/performances/{world['performance_id']}/orders"
        body = {"seat_uids": ["A-3"], "external_ref": "cart-once"}
        first = api_client.post(url, json=body, headers=role_header("box_office"))
        second = api_client.post(url, json=body, headers=role_header("box_office"))
        assert first.json()["id"] == second.json()["id"]

    def test_a_taken_seat_returns_the_services_conflict_payload_verbatim(
        self, api_client, published, world
    ):
        url = f"/v1/performances/{world['performance_id']}/orders"
        first = api_client.post(
            url, json={"seat_uids": ["A-5"]}, headers=role_header("box_office")
        ).json()

        response = api_client.post(
            url, json={"seat_uids": ["A-5", "A-6"]}, headers=role_header("box_office")
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "seats_unavailable"
        # `conflicts`, not `detail` - the documented variation on the envelope.
        assert set(body) == {"error", "message", "conflicts"}
        assert len(body["conflicts"]) == 1, "only the OFFENDING seat is reported"

        conflict = body["conflicts"][0]
        assert set(conflict) == {"seat_uid", "reason", "seat_id", "detail"}
        assert conflict["seat_uid"] == "A-5"
        assert conflict["reason"] == "locked"
        assert conflict["detail"]["held_by_order_id"] == first["id"]
        assert conflict["detail"]["held_until"] == first["expires_at"]

    def test_a_lost_basket_holds_nothing(self, api_client, published, world):
        url = f"/v1/performances/{world['performance_id']}/orders"
        api_client.post(url, json={"seat_uids": ["A-5"]}, headers=role_header("box_office"))
        api_client.post(
            url, json={"seat_uids": ["A-5", "A-6"]}, headers=role_header("box_office")
        )
        seats = {
            s["uid"]: s["status"]
            for s in api_client.get(
                f"/v1/performances/{world['performance_id']}/availability",
                headers=role_header("marketing"),
            ).json()["seats"]
        }
        assert seats["A-6"] == "available", "all-or-nothing: nothing was partially held"

    def test_a_blocked_seat_reports_seat_status(self, api_client, published, world):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/orders",
            json={"seat_uids": ["F-6"]},
            headers=role_header("box_office"),
        )
        assert response.status_code == 409
        conflict = response.json()["conflicts"][0]
        assert (conflict["seat_uid"], conflict["reason"]) == ("F-6", "seat_status")

    def test_an_unknown_seat_is_a_conflict_not_a_404(
        self, api_client, published, world
    ):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/orders",
            json={"seat_uids": ["Z-99"]},
            headers=role_header("box_office"),
        )
        assert response.status_code == 409
        conflict = response.json()["conflicts"][0]
        assert (conflict["seat_uid"], conflict["reason"]) == ("Z-99", "unknown_seat")


class TestOrderLifecycle:
    def _order(self, api_client, world, seats=("A-1",)):
        return api_client.post(
            f"/v1/performances/{world['performance_id']}/orders",
            json={"seat_uids": list(seats)},
            headers=role_header("box_office"),
        ).json()

    def test_extend_adds_four_minutes_exactly_once(
        self, api_client, published, world, manual_clock
    ):
        order = self._order(api_client, world)
        first = api_client.post(
            f"/v1/orders/{order['id']}/extend", headers=role_header("box_office")
        )
        assert first.status_code == 200
        assert first.json()["extended"] is True
        assert first.json()["expires_at"] == (
            manual_clock.now() + timedelta(minutes=12)
        ).isoformat()

        second = api_client.post(
            f"/v1/orders/{order['id']}/extend", headers=role_header("box_office")
        )
        assert second.status_code == 409
        assert envelope(second)["error"] == "extension_already_used"

    def test_an_expired_hold_cannot_be_extended(
        self, api_client, published, world, manual_clock
    ):
        order = self._order(api_client, world)
        manual_clock.advance(timedelta(minutes=9))
        response = api_client.post(
            f"/v1/orders/{order['id']}/extend", headers=role_header("box_office")
        )
        assert response.status_code == 409
        body = envelope(response)
        assert body["error"] == "order_not_live"
        assert body["detail"]["expired"] is True

    def test_release_frees_the_seats_immediately(self, api_client, published, world):
        order = self._order(api_client, world, ("A-1", "A-2"))
        response = api_client.post(
            f"/v1/orders/{order['id']}/release",
            json={"reason": "customer abandoned"},
            headers=role_header("box_office"),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

        seats = {
            s["uid"]: s["status"]
            for s in api_client.get(
                f"/v1/performances/{world['performance_id']}/availability",
                headers=role_header("marketing"),
            ).json()["seats"]
        }
        assert seats["A-1"] == "available" and seats["A-2"] == "available"

    def test_releasing_twice_is_not_an_error(self, api_client, published, world):
        order = self._order(api_client, world)
        url = f"/v1/orders/{order['id']}/release"
        assert api_client.post(url, headers=role_header("box_office")).status_code == 200
        assert api_client.post(url, headers=role_header("box_office")).status_code == 200

    def test_complete_issues_tickets_and_returns_credentials_once(
        self, api_client, published, world
    ):
        order = self._order(api_client, world, ("A-1", "A-2"))
        response = api_client.post(
            f"/v1/orders/{order['id']}/complete", headers=role_header("box_office")
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["total_minor"] == 2 * PRICES["vip"]
        assert [t["seat_uid"] for t in body["tickets"]] == ["A-1", "A-2"]
        assert all(t["credential"].startswith("kt1.") for t in body["tickets"])

        listed = api_client.get(
            f"/v1/orders/{order['id']}/tickets", headers=role_header("box_office")
        ).json()
        assert listed["total"] == 2
        assert all("credential" not in t for t in listed["items"])
        assert {t["seat_uid"] for t in listed["items"]} == {"A-1", "A-2"}

    def test_completing_an_expired_order_is_refused(
        self, api_client, published, world, manual_clock
    ):
        order = self._order(api_client, world)
        manual_clock.advance(timedelta(minutes=9))
        response = api_client.post(
            f"/v1/orders/{order['id']}/complete", headers=role_header("box_office")
        )
        assert response.status_code == 409
        body = envelope(response)
        assert body["error"] == "order_not_live"
        assert body["detail"]["expired"] is True

    def test_a_completed_order_cannot_be_released(self, api_client, published, world):
        order = self._order(api_client, world)
        api_client.post(
            f"/v1/orders/{order['id']}/complete", headers=role_header("box_office")
        )
        response = api_client.post(
            f"/v1/orders/{order['id']}/release", headers=role_header("box_office")
        )
        assert response.status_code == 409
        assert envelope(response)["error"] == "order_not_live"


# ── Tickets ─────────────────────────────────────────────────────────────────
class TestTickets:
    @pytest.fixture
    def issued(self, api_client, published, world):
        order = api_client.post(
            f"/v1/performances/{world['performance_id']}/orders",
            json={"seat_uids": ["A-1"]},
            headers=role_header("box_office"),
        ).json()
        completed = api_client.post(
            f"/v1/orders/{order['id']}/complete", headers=role_header("box_office")
        ).json()
        return completed["tickets"][0]

    def test_rotation_keeps_the_ticket_and_replaces_the_credential(
        self, api_client, issued
    ):
        before = api_client.get(
            f"/v1/tickets/{issued['ticket_id']}", headers=role_header("support")
        ).json()
        assert before["credential_version"] == 1

        response = api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/rotate-credential",
            json={"reason": "customer lost their phone"},
            headers=role_header("support"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["credential_version"] == 2
        assert body["credential"] != issued["credential"]

        after = api_client.get(
            f"/v1/tickets/{issued['ticket_id']}", headers=role_header("support")
        ).json()
        assert after["id"] == before["id"]
        assert after["status"] == before["status"] == "issued"
        assert after["credential_version"] == 2

    def test_finance_may_refund_but_not_rotate(self, api_client, issued):
        rotate = api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/rotate-credential",
            headers=role_header("finance"),
        )
        assert rotate.status_code == 403
        refund = api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/refund", headers=role_header("finance")
        )
        assert refund.status_code == 200
        assert refund.json()["status"] == "refunded"

    def test_cancelling_frees_the_seat_but_keeps_the_usage_row(
        self, api_client, issued, world, session
    ):
        response = api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/cancel",
            json={"reason": "duplicate sale"},
            headers=role_header("box_office"),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["seat_uid"] == "A-1"

        seats = {
            s["uid"]: s["status"]
            for s in api_client.get(
                f"/v1/performances/{world['performance_id']}/availability",
                headers=role_header("marketing"),
            ).json()["seats"]
        }
        assert seats["A-1"] == "available"

        usage = session.execute(select(em.UsageEvent)).scalars().all()
        assert len(usage) == 1, "issuing consumed quota; cancelling does not refund it"

    def test_a_terminal_ticket_refuses_further_transitions(self, api_client, issued):
        api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/cancel", headers=role_header("box_office")
        )
        response = api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/refund", headers=role_header("box_office")
        )
        assert response.status_code == 409
        assert envelope(response)["error"] == "invalid_ticket_transition"


# ── Check-in ────────────────────────────────────────────────────────────────
class TestCheckIn:
    @pytest.fixture
    def issued(self, api_client, published, world):
        order = api_client.post(
            f"/v1/performances/{world['performance_id']}/orders",
            json={"seat_uids": ["A-1"]},
            headers=role_header("box_office"),
        ).json()
        return api_client.post(
            f"/v1/orders/{order['id']}/complete", headers=role_header("box_office")
        ).json()["tickets"][0]

    def scan(self, api_client, credential, performance_id=None, role="scanner"):
        payload = {"credential": credential}
        if performance_id is not None:
            payload["performance_id"] = performance_id
        return api_client.post("/v1/checkin", json=payload, headers=role_header(role))

    def test_valid(self, api_client, issued, world):
        response = self.scan(api_client, issued["credential"], world["performance_id"])
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == "valid"
        assert body["ticket_id"] == issued["ticket_id"]
        assert body["seat_uid"] == "A-1"
        assert body["checked_in_at"] is not None

    def test_already_checked_in(self, api_client, issued, world):
        self.scan(api_client, issued["credential"], world["performance_id"])
        response = self.scan(api_client, issued["credential"], world["performance_id"])
        assert response.status_code == 200
        assert response.json()["verdict"] == "already_checked_in"
        assert response.json()["checked_in_at"] is not None

    def test_superseded_after_rotation(self, api_client, issued, world):
        api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/rotate-credential",
            headers=role_header("support"),
        )
        stale = self.scan(api_client, issued["credential"], world["performance_id"])
        assert stale.json()["verdict"] == "superseded"

    def test_the_rotated_credential_is_the_one_that_works(
        self, api_client, issued, world
    ):
        rotated = api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/rotate-credential",
            headers=role_header("support"),
        ).json()
        response = self.scan(api_client, rotated["credential"], world["performance_id"])
        assert response.json()["verdict"] == "valid"

    def test_cancelled(self, api_client, issued, world):
        api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/cancel", headers=role_header("box_office")
        )
        response = self.scan(api_client, issued["credential"], world["performance_id"])
        assert response.json()["verdict"] == "cancelled"

    def test_refunded(self, api_client, issued, world):
        api_client.post(
            f"/v1/tickets/{issued['ticket_id']}/refund", headers=role_header("finance")
        )
        response = self.scan(api_client, issued["credential"], world["performance_id"])
        assert response.json()["verdict"] == "refunded"

    def test_wrong_performance(self, api_client, issued):
        response = self.scan(api_client, issued["credential"], performance_id=999_999)
        assert response.json()["verdict"] == "wrong_performance"

    def test_invalid_for_a_forged_token(self, api_client, issued, world):
        forged = issued["credential"][:-4] + "AAAA"
        response = self.scan(api_client, forged, world["performance_id"])
        assert response.json()["verdict"] == "invalid"
        assert response.json()["ticket_id"] is None

    def test_invalid_for_a_token_that_is_not_ours_at_all(self, api_client, world):
        response = self.scan(api_client, "hello", world["performance_id"])
        assert response.json()["verdict"] == "invalid"

    def test_another_organizations_scanner_sees_only_invalid(
        self, api_client, issued, world
    ):
        """Never `wrong_performance` or a 403 - that would confirm the ticket."""
        from tests.engine.conftest import user_header

        response = api_client.post(
            "/v1/checkin",
            json={"credential": issued["credential"]},
            headers=user_header("test|outsider"),
        )
        assert response.status_code == 200
        assert response.json()["verdict"] == "invalid"

    def test_a_member_without_the_scanner_tier_is_403(self, api_client, issued, world):
        response = self.scan(
            api_client, issued["credential"], world["performance_id"], role="marketing"
        )
        assert response.status_code == 403
        assert envelope(response)["error"] == "insufficient_role"

    def test_an_api_key_may_run_a_turnstile(
        self, api_client, issued, world, make_api_key
    ):
        token = make_api_key(world["org_id"], scopes=["write"])
        response = api_client.post(
            "/v1/checkin",
            json={"credential": issued["credential"],
                  "performance_id": world["performance_id"]},
            headers=key_header(token),
        )
        assert response.status_code == 200
        assert response.json()["verdict"] == "valid"

    def test_a_read_only_key_may_not(self, api_client, issued, world, make_api_key):
        token = make_api_key(world["org_id"], scopes=["read"])
        response = api_client.post(
            "/v1/checkin",
            json={"credential": issued["credential"]},
            headers=key_header(token),
        )
        assert response.status_code == 403
        assert envelope(response)["error"] == "insufficient_scope"


# ── Cross-cutting ───────────────────────────────────────────────────────────
class TestErrorEnvelopeIsUniform:
    @pytest.mark.parametrize(
        "method,path,payload,expected",
        [
            ("get", "/v1/orgs/999999", None, 404),
            ("get", "/v1/orders/999999", None, 404),
            ("get", "/v1/tickets/999999", None, 404),
            ("get", "/v1/performances/999999", None, 404),
        ],
    )
    def test_missing_resources_share_one_shape(
        self, api_client, method, path, payload, expected
    ):
        response = getattr(api_client, method)(path, headers=role_header("owner"))
        assert response.status_code == expected
        body = envelope(response)
        assert body["error"] == "not_found"

    def test_a_malformed_body_is_422_in_the_envelope(self, api_client, published, world):
        response = api_client.post(
            f"/v1/performances/{world['performance_id']}/orders",
            json={"seat_uids": []},
            headers=role_header("box_office"),
        )
        assert response.status_code == 422
        body = envelope(response)
        assert body["error"] == "invalid_request"
        assert isinstance(body["detail"]["errors"], list)

    def test_no_response_ever_carries_a_traceback(self, api_client, world):
        for path in ("/v1/orgs/999999", "/v1/orders/999999"):
            text = api_client.get(path, headers=role_header("owner")).text
            assert "Traceback" not in text
            assert "sqlalchemy" not in text.lower()


class TestDatabaseErrorMapping:
    """The two database-level rejections, mapped by `app.api.errors`.

    The frozen-layout one is reachable through a route and is asserted that way
    above. The integer-money one is a BACKSTOP: every /v1 field that carries
    money today is validated by `pricing.normalize_prices` long before a column
    sees it, so nothing currently gets that far. It is still installed, because
    the day a field does reach `MinorAmount` directly, a bind-time TypeError
    must not become a 500. These call the registered handlers with real
    SQLAlchemy exceptions, so what is asserted is the code that is actually
    wired to the app rather than a re-implementation of it.
    """

    def _handler(self, exc_type):
        from app.main import app

        for cls, handler in app.exception_handlers.items():
            if cls is exc_type:
                return handler
        raise AssertionError(f"no handler registered for {exc_type}")

    def _request(self, path):
        from starlette.requests import Request

        return Request(
            {"type": "http", "method": "GET", "path": path, "headers": [],
             "query_string": b"", "scheme": "http", "server": ("test", 80),
             "root_path": "", "client": ("test", 1)}
        )

    async def _call(self, exc_type, exc, path):
        return await self._handler(exc_type)(self._request(path), exc)

    def test_a_bind_time_money_typeerror_becomes_422(self):
        import asyncio
        import json

        from sqlalchemy.exc import StatementError

        exc = StatementError(
            "monetary value must be an integer number of minor units, got "
            "float: 25.5",
            "UPDATE engine_orders SET total_minor=?",
            (), TypeError("minor units"),
        )
        response = asyncio.run(self._call(StatementError, exc, "/v1/anything"))
        assert response.status_code == 422
        body = json.loads(response.body)
        assert body["error"] == "invalid_request"
        assert "integer number of minor units" in body["message"]

    def test_an_unrecognised_database_error_is_a_bare_500(self):
        import asyncio
        import json

        from sqlalchemy.exc import DBAPIError

        exc = DBAPIError(
            "SELECT secret_column FROM engine_orders",
            (), Exception("connection reset by peer"),
        )
        response = asyncio.run(self._call(DBAPIError, exc, "/v1/anything"))
        assert response.status_code == 500
        body = json.loads(response.body)
        assert body == {
            "error": "internal_error",
            "message": "the request could not be completed",
        }
        assert "secret_column" not in json.dumps(body)

    def test_the_handlers_refuse_to_touch_a_legacy_path(self):
        import asyncio

        from sqlalchemy.exc import DBAPIError

        exc = DBAPIError("SELECT 1", (), Exception("boom"))
        with pytest.raises(DBAPIError):
            asyncio.run(self._call(DBAPIError, exc, "/events/1"))
