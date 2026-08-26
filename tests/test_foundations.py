"""Phase-0 foundation tests.

These exist to prove the harness works end to end - a fresh schema, seeded data,
a real HTTP round trip through the app, and JWT verification mocked at the
dependency boundary. They are deliberately narrow; they are not full coverage.

Covered here:
  a) auth rejects missing and invalid tokens
  b) GET /events only exposes PUBLIC_STATUSES events
  c) GET /events/{id} returns seats + categories with the expected fields
  d) get_current_user auto-provisions a User row on first authenticated request
"""
from __future__ import annotations

import pytest

from app.models import Event, User
from app.routers.events import PUBLIC_STATUSES
from tests.conftest import auth_header


# ── a) Auth ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/users/me", "/events/mine/list"])
def test_request_without_token_is_401(client, path):
    """No Authorization header at all -> 401, never a 500 or a silent pass."""
    r = client.get(path)
    assert r.status_code == 401


@pytest.mark.parametrize("bad_token", ["not-a-jwt", "Bearer-ish garbage", ""])
def test_request_with_invalid_token_is_401(client, fake_jwt, bad_token):
    """A token that fails verification -> 401. `fake_jwt` rejects anything that
    isn't one of our fixture markers, standing in for a real signature failure."""
    r = client.get("/users/me", headers={"Authorization": f"Bearer {bad_token}"})
    assert r.status_code == 401


def test_valid_token_is_accepted(client, fake_jwt):
    """Control case - proves the 401s above come from verification, not from the
    route being unreachable."""
    r = client.get("/users/me", headers=auth_header(sub="test|valid-user"))
    assert r.status_code == 200
    assert r.json()["auth0_sub"] == "test|valid-user"


# ── b) Public event listing ─────────────────────────────────────────────────
def test_list_events_returns_only_public_statuses(client, db, seed):
    """One event per status; only PUBLIC_STATUSES ones may appear in the listing."""
    organizer_id = seed["user"].id
    all_statuses = ["active", "coming_soon", "scheduled", "draft", "inactive"]

    # `seed` already provides one 'active' event; add the rest.
    for status_value in all_statuses:
        if status_value == "active":
            continue
        db.add(
            Event(
                organizer_id=organizer_id,
                name=f"Event {status_value}",
                status=status_value,
                seats=[],
                categories=[],
                gallery=[],
            )
        )
    db.commit()

    r = client.get("/events")
    assert r.status_code == 200
    body = r.json()

    returned_ids = [e["id"] for e in body["events"]]
    returned_statuses = {e["status"] for e in body["events"]}

    assert returned_statuses <= set(PUBLIC_STATUSES), (
        f"Non-public statuses leaked into the listing: "
        f"{returned_statuses - set(PUBLIC_STATUSES)}"
    )
    # Every hidden event must genuinely be absent.
    hidden = db.query(Event).filter(Event.status.in_(["draft", "inactive"])).all()
    for event in hidden:
        assert event.id not in returned_ids

    assert body["total"] == len(PUBLIC_STATUSES) == 3
    assert body["page"] == 1


# ── c) Event detail ─────────────────────────────────────────────────────────
def test_event_detail_returns_seats_and_categories(client, seed):
    event = seed["event"]
    r = client.get(f"/events/{event.id}")
    assert r.status_code == 200
    body = r.json()

    assert body["id"] == event.id
    assert body["name"] == "Seeded Test Event"

    # Seats: full seat-builder shape.
    assert len(body["seats"]) == 6
    seat = body["seats"][0]
    for field in ("id", "x", "y", "catId", "row", "col", "label", "blocked"):
        assert field in seat, f"seat missing {field!r}"

    # Categories: id/name/price/color.
    assert len(body["categories"]) == 2
    category = body["categories"][0]
    for field in ("id", "name", "price", "color"):
        assert field in category, f"category missing {field!r}"

    # Detail-only extras added by EventDetailOut.
    assert body["venue_info"]["name"] == "Test Hall"
    assert body["price_range"] == {"min": 45.0, "max": 120.0}
    assert body["related_events"] == []
    assert body["capacity"] == 6


def test_event_detail_404_for_missing_event(client):
    assert client.get("/events/999999").status_code == 404


# ── d) Auto-provisioning ────────────────────────────────────────────────────
def test_get_current_user_auto_provisions_a_user_row(client, db, fake_jwt):
    """A first-time login must create the local User row from the token claims."""
    sub = "auth0|brand-new-user"
    assert db.query(User).filter(User.auth0_sub == sub).first() is None

    r = client.get(
        "/users/me", headers=auth_header(sub=sub, role="organizer", email="new@kursi.io")
    )
    assert r.status_code == 200

    db.expire_all()
    created = db.query(User).filter(User.auth0_sub == sub).one()
    assert created.email == "new@kursi.io"
    assert created.role == "organizer"
    assert r.json()["id"] == created.id


def test_auto_provisioning_is_idempotent(client, db, fake_jwt):
    """A second request with the same sub must reuse the row, not duplicate it."""
    sub = "auth0|repeat-user"
    first = client.get("/users/me", headers=auth_header(sub=sub))
    second = client.get("/users/me", headers=auth_header(sub=sub))

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    db.expire_all()
    assert db.query(User).filter(User.auth0_sub == sub).count() == 1


# ── Health / environment awareness ──────────────────────────────────────────
def test_health_reports_env_and_migration_fields(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["env"] == "development"
    # Present even when the test DB was built by create_all rather than Alembic.
    for field in ("version", "db_revision", "head_revision", "migration_state"):
        assert field in body
    assert body["migration_state"] in ("up_to_date", "out_of_date", "unknown")
