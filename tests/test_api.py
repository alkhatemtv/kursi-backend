"""Integration tests for the Kursi API.

Auth0 token verification is mocked so we can exercise full request flows without
needing a real Auth0 tenant. Run with:
    pytest tests/

If pytest isn't installed: pip install pytest
"""
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path before importing app.*
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use a temp file-based SQLite so all connections share the same database.
# Pure :memory: won't work because each SQLAlchemy connection gets its own DB.
TEST_DB_PATH = ROOT / "test_kursi.db"
if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["AUTH0_DOMAIN"] = "test.auth0.com"
os.environ["AUTH0_API_AUDIENCE"] = "https://api.kursi.io"

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.database import Base, engine
from app.main import app


def make_token(role: str = "customer", sub: str = "test|user-1", email: str | None = None) -> str:
    """Just an opaque marker — `_decode_token` is monkey-patched to return claims based on it."""
    return f"FAKE.{sub}.{role}.{email or sub + '@example.com'}"


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset the in-memory database between tests."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def patch_auth(monkeypatch):
    """Replace _decode_token with a parser that reads our fake token format."""
    def fake_decode(token: str):
        if not token.startswith("FAKE."):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid token")
        _, sub, role, email = token.split(".", 3)
        return {
            "sub": sub,
            "email": email,
            "https://kursi.io/role": role,
            "name": email.split("@")[0],
        }
    monkeypatch.setattr(auth, "_decode_token", fake_decode)


@pytest.fixture
def client():
    return TestClient(app)


def auth_header(role="customer", sub="test|user-1", email=None):
    return {"Authorization": f"Bearer {make_token(role, sub, email)}"}


# ── Public ──────────────────────────────────────────────────
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_public_events_empty(client):
    r = client.get("/events")
    assert r.status_code == 200
    body = r.json()
    assert body["events"] == []
    assert body["total"] == 0
    assert body["page"] == 1
    assert body["per_page"] == 20
    assert body["total_pages"] == 0


# ── Auth ────────────────────────────────────────────────────
def test_users_me_creates_user_on_first_call(client):
    r = client.get("/users/me", headers=auth_header(role="organizer"))
    assert r.status_code == 200
    body = r.json()
    assert body["auth0_sub"] == "test|user-1"
    assert body["role"] == "organizer"


def test_protected_route_rejects_no_token(client):
    r = client.get("/users/me")
    # FastAPI HTTPBearer auto-error: was 403 in older versions, 401 in 0.118+.
    assert r.status_code in (401, 403)


def test_protected_route_rejects_bad_token(client):
    r = client.get("/users/me", headers={"Authorization": "Bearer not-fake-prefix"})
    assert r.status_code == 401


# ── Events ──────────────────────────────────────────────────
def test_create_event_requires_organizer(client):
    payload = {"name": "Test Event"}
    r = client.post("/events", json=payload, headers=auth_header(role="customer"))
    assert r.status_code == 403


def test_create_and_list_event(client):
    payload = {
        "name": "My Show",
        "venue": "The Hall",
        "icon": "🎭",
        "tag": "Theater",
        "status": "active",
        "categories": [{"id": "g", "name": "Gold", "price": 50, "color": "#f59e0b"}],
        "seats": [
            {"id": "1", "x": 0, "y": 0, "catId": "g", "label": "GA1"},
            {"id": "2", "x": 32, "y": 0, "catId": "g", "label": "GA2"},
        ],
    }
    r = client.post("/events", json=payload, headers=auth_header(role="organizer"))
    assert r.status_code == 201, r.text
    event = r.json()
    assert event["name"] == "My Show"
    assert event["capacity"] == 2
    assert event["sold_count"] == 0

    # Should appear on the public list (status=active)
    r = client.get("/events")
    body = r.json()
    assert body["total"] == 1
    assert len(body["events"]) == 1


def test_update_event_only_by_owner(client):
    payload = {
        "name": "First", "status": "active",
        "categories": [{"id": "g", "name": "Gold", "price": 50, "color": "#f59e0b"}],
        "seats": [],
    }
    r = client.post("/events", json=payload, headers=auth_header(role="organizer", sub="test|owner"))
    event_id = r.json()["id"]

    # Different organizer can't update
    r = client.put(
        f"/events/{event_id}",
        json={"name": "Hijacked"},
        headers=auth_header(role="organizer", sub="test|other"),
    )
    assert r.status_code == 403

    # Owner can
    r = client.put(
        f"/events/{event_id}",
        json={"name": "Updated"},
        headers=auth_header(role="organizer", sub="test|owner"),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Updated"


def test_delete_event(client):
    payload = {"name": "Goner", "status": "active", "categories": [], "seats": []}
    r = client.post("/events", json=payload, headers=auth_header(role="organizer"))
    event_id = r.json()["id"]
    r = client.delete(f"/events/{event_id}", headers=auth_header(role="organizer"))
    assert r.status_code == 200
    r = client.get(f"/events/{event_id}")
    assert r.status_code == 404


# ── Bookings + Refunds ──────────────────────────────────────
def test_full_booking_and_refund_flow(client):
    # Organizer creates event
    payload = {
        "name": "Concert", "status": "active",
        "categories": [{"id": "g", "name": "Gold", "price": 50, "color": "#f59e0b"}],
        "seats": [
            {"id": "1", "x": 0, "y": 0, "catId": "g", "label": "GA1"},
            {"id": "2", "x": 32, "y": 0, "catId": "g", "label": "GA2"},
        ],
    }
    r = client.post("/events", json=payload, headers=auth_header(role="organizer", sub="test|org"))
    event_id = r.json()["id"]

    # Customer books seat GA1
    r = client.post(
        "/bookings",
        json={
            "event_id": event_id,
            "seats": [{"label": "GA1", "category": "Gold", "price": 50}],
        },
        headers=auth_header(role="customer", sub="test|cust"),
    )
    assert r.status_code == 201, r.text
    booking = r.json()
    assert booking["total"] == 50
    assert booking["status"] == "confirmed"
    assert booking["ref"].startswith("KURSI-")
    booking_id = booking["id"]

    # Same seat now conflicts
    r = client.post(
        "/bookings",
        json={"event_id": event_id, "seats": [{"label": "GA1", "category": "Gold", "price": 50}]},
        headers=auth_header(role="customer", sub="test|cust2"),
    )
    assert r.status_code == 409

    # Organizer can see the booking
    r = client.get(f"/bookings/event/{event_id}", headers=auth_header(role="organizer", sub="test|org"))
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Customer can list their bookings
    r = client.get("/bookings/mine", headers=auth_header(role="customer", sub="test|cust"))
    assert len(r.json()) == 1

    # Organizer initiates refund
    r = client.post(
        "/refunds",
        json={"booking_id": booking_id, "reason": "Show cancelled"},
        headers=auth_header(role="organizer", sub="test|org"),
    )
    assert r.status_code == 201
    refund_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    # Approve it
    r = client.post(f"/refunds/{refund_id}/approve", headers=auth_header(role="organizer", sub="test|org"))
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    # Booking should now be refunded — and the seat should be available again
    r = client.post(
        "/bookings",
        json={"event_id": event_id, "seats": [{"label": "GA1", "category": "Gold", "price": 50}]},
        headers=auth_header(role="customer", sub="test|cust3"),
    )
    assert r.status_code == 201, "Seat should be available after refund"


# ── Search / filter / sort / pagination ─────────────────────
def _seed_catalog(client):
    """Seed three active events with distinct dates / tags / prices for filter tests."""
    events = [
        {
            "name": "Verdi Opera Night",
            "venue": "Grand Hall",
            "performer": "Soprano X",
            "tag": "Theater",
            "event_date": "2026-06-10",
            "status": "active",
            "categories": [{"id": "a", "name": "A", "price": 80, "color": "#000"}],
            "seats": [{"id": "1", "x": 0, "y": 0, "catId": "a", "label": "A1"}],
        },
        {
            "name": "Indie Film Marathon",
            "venue": "Cinema One",
            "performer": "Various",
            "tag": "Cinema",
            "event_date": "2026-07-05",
            "status": "active",
            "categories": [{"id": "a", "name": "A", "price": 20, "color": "#000"}],
            "seats": [{"id": "1", "x": 0, "y": 0, "catId": "a", "label": "A1"}],
        },
        {
            "name": "Shakespeare in the Park",
            "venue": "Open Air Theatre",
            "performer": "Royal Players",
            "tag": "Theater",
            "event_date": "2026-06-20",
            "status": "active",
            "categories": [{"id": "a", "name": "A", "price": 35, "color": "#000"}],
            "seats": [{"id": "1", "x": 0, "y": 0, "catId": "a", "label": "A1"}],
        },
    ]
    for payload in events:
        r = client.post("/events", json=payload, headers=auth_header(role="organizer"))
        assert r.status_code == 201, r.text


def test_events_search_matches_name_venue_and_performer(client):
    _seed_catalog(client)

    r = client.get("/events?search=opera")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["events"]]
    assert names == ["Verdi Opera Night"]

    r = client.get("/events?search=cinema")  # matches venue "Cinema One"
    assert [e["name"] for e in r.json()["events"]] == ["Indie Film Marathon"]

    r = client.get("/events?search=royal")  # matches performer "Royal Players"
    assert [e["name"] for e in r.json()["events"]] == ["Shakespeare in the Park"]


def test_events_filter_by_category_and_sort_price_asc(client):
    _seed_catalog(client)
    r = client.get("/events?category=Theater&sort=price_asc")
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["name"] for e in events] == [
        "Shakespeare in the Park",  # 35
        "Verdi Opera Night",        # 80
    ]


def test_events_filter_by_date_range(client):
    _seed_catalog(client)
    r = client.get("/events?date_from=2026-06-01&date_to=2026-06-30")
    assert r.status_code == 200
    names = sorted(e["name"] for e in r.json()["events"])
    assert names == ["Shakespeare in the Park", "Verdi Opera Night"]


def test_events_pagination(client):
    _seed_catalog(client)
    r = client.get("/events?page=1&per_page=2&sort=date_asc")
    body = r.json()
    assert body["total"] == 3
    assert body["per_page"] == 2
    assert body["total_pages"] == 2
    assert len(body["events"]) == 2

    r = client.get("/events?page=2&per_page=2&sort=date_asc")
    assert len(r.json()["events"]) == 1


def test_events_invalid_sort_returns_400_with_error_shape(client):
    r = client.get("/events?sort=bogus")
    assert r.status_code == 400
    assert "error" in r.json()


def test_events_per_page_over_max_returns_400(client):
    r = client.get("/events?per_page=51")
    assert r.status_code == 400
    assert "error" in r.json()


def test_events_invalid_date_returns_400(client):
    r = client.get("/events?date_from=not-a-date")
    assert r.status_code == 400
    assert "error" in r.json()


def test_events_no_match_returns_empty_list_not_404(client):
    _seed_catalog(client)
    r = client.get("/events?search=zzznonexistent")
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["events"] == []


# ── Event detail ────────────────────────────────────────────
def test_event_detail_returns_full_payload_and_increments_views(client):
    _seed_catalog(client)
    listing = client.get("/events?search=opera").json()["events"]
    eid = listing[0]["id"]

    r = client.get(f"/events/{eid}")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=300"
    body = r.json()
    assert body["name"] == "Verdi Opera Night"
    assert body["venue_info"] == {"name": "Grand Hall"}
    assert body["price_range"] == {"min": 80.0, "max": 80.0}
    assert body["view_count"] == 1
    # Same tag, different event → should appear in related
    related_names = [e["name"] for e in body["related_events"]]
    assert "Shakespeare in the Park" in related_names

    # Second fetch increments view_count
    r = client.get(f"/events/{eid}")
    assert r.json()["view_count"] == 2


def test_event_detail_404(client):
    r = client.get("/events/9999")
    assert r.status_code == 404


# ── Wishlist ────────────────────────────────────────────────
def test_wishlist_requires_auth(client):
    r = client.get("/wishlist")
    assert r.status_code in (401, 403)
    r = client.post("/wishlist", json={"event_id": 1})
    assert r.status_code in (401, 403)


def test_wishlist_add_list_remove(client):
    _seed_catalog(client)
    eid = client.get("/events?search=opera").json()["events"][0]["id"]

    r = client.post("/wishlist", json={"event_id": eid}, headers=auth_header(role="customer", sub="test|cust"))
    assert r.status_code == 201, r.text
    assert r.json()["event_id"] == eid

    # Idempotent — adding the same event again doesn't error or duplicate.
    r = client.post("/wishlist", json={"event_id": eid}, headers=auth_header(role="customer", sub="test|cust"))
    assert r.status_code == 201

    r = client.get("/wishlist", headers=auth_header(role="customer", sub="test|cust"))
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["event"]["name"] == "Verdi Opera Night"

    r = client.delete(f"/wishlist/{eid}", headers=auth_header(role="customer", sub="test|cust"))
    assert r.status_code == 200

    r = client.get("/wishlist", headers=auth_header(role="customer", sub="test|cust"))
    assert r.json() == []
