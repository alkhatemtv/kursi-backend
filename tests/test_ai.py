"""Integration tests for the AI layout endpoints.

Auth0 verification and the Anthropic SDK call are both mocked so this suite
runs offline. Run with:
    pytest tests/test_ai.py -v
"""
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Mirror the fixture setup from test_api.py — same temp DB + Auth0 stubs.
TEST_DB_PATH = ROOT / "test_kursi_ai.db"
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
from app.routers import ai as ai_router


# ── Helpers ─────────────────────────────────────────────────
def _make_token(sub: str = "test|ai-user") -> str:
    return f"FAKE.{sub}.customer.{sub}@example.com"


def _auth_header(sub: str = "test|ai-user") -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_token(sub)}"}


def _u() -> str:
    """Random UUID v4 string."""
    return str(uuid.uuid4())


def _valid_layout(name: str = "Mock Layout") -> dict:
    """A minimal v2-valid layout — used as a stub for the mocked Anthropic response."""
    sec_id = _u()
    layer_seat = _u()
    cat_id = _u()
    return {
        "schema_version": "2.0.0",
        "venue": {
            "id": _u(),
            "name": name,
            "type": "theater",
            "dimensions": {"width_m": 15.0, "depth_m": 12.0},
            "owner_id": _u(),
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        },
        "sections": [{
            "id": sec_id, "name": "main_floor", "label": "Main Floor",
            "origin": {"x": 0, "y": 0}, "bounds": {"width": 750, "height": 600},
            "rotation_deg": 0,
        }],
        "seats": [{
            "id": _u(), "section_id": sec_id, "x": 100, "y": 100,
            "row": "A", "number": "1", "category_id": cat_id,
            "price_override": None,
            "accessibility": {"wheelchair": False, "companion": False},
            "seat_type": "standard", "status": "available", "notes": "",
        }],
        "categories": [{"id": cat_id, "name": "General", "color": "#94a3b8", "default_price": 10.0}],
        "objects": [{
            "id": _u(), "type": "stage", "section_id": None,
            "x": 100, "y": 30, "width": 500, "height": 50,
            "rotation_deg": 0, "label": "STAGE", "z_index": 0, "layer_id": layer_seat,
        }],
        "layers": [
            {"id": _u(), "name": "stage", "visible": True, "locked": False, "z_order": 0},
            {"id": layer_seat, "name": "seating", "visible": True, "locked": False, "z_order": 10},
            {"id": _u(), "name": "aisles", "visible": True, "locked": False, "z_order": 20},
            {"id": _u(), "name": "labels", "visible": True, "locked": False, "z_order": 30},
        ],
    }


# ── Fixtures ────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def patch_auth(monkeypatch):
    def fake_decode(token: str):
        if not token.startswith("FAKE."):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid token")
        _, sub, role, email = token.split(".", 3)
        return {
            "sub": sub, "email": email,
            "https://kursi.io/role": role, "name": email.split("@")[0],
        }
    monkeypatch.setattr(auth, "_decode_token", fake_decode)


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    ai_router._reset_rate_limits_for_testing()
    yield
    ai_router._reset_rate_limits_for_testing()


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    yield


@pytest.fixture
def without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


@pytest.fixture
def stub_anthropic(monkeypatch):
    """Replace _call_anthropic with a function returning a serialized valid layout."""
    def fake(system, user_content):
        return json.dumps(_valid_layout())
    monkeypatch.setattr(ai_router, "_call_anthropic", fake)
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ── Tests ───────────────────────────────────────────────────
def test_generate_requires_jwt(client, with_api_key):
    # No Authorization header at all → FastAPI's HTTPBearer auto-error
    r = client.post("/api/ai/generate-layout", json={
        "venue_type": "theater",
        "dimensions": {"width": 15, "depth": 12},
        "seat_count": 200,
    })
    assert r.status_code in (401, 403)

    # Authorization header present but invalid → our verifier raises 401
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200},
        headers={"Authorization": "Bearer NOPE"},
    )
    assert r.status_code == 401


def test_generate_503_when_api_key_missing(client, without_api_key):
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200},
        headers=_auth_header(),
    )
    assert r.status_code == 503
    assert "temporarily unavailable" in r.json()["detail"].lower()


def test_generate_400_on_invalid_venue_type(client, with_api_key):
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "rooftop_garden", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200},
        headers=_auth_header(),
    )
    # pydantic and our extra check both produce 4xx; we accept 400 or 422 here.
    assert r.status_code in (400, 422)


def test_generate_400_on_negative_dimensions(client, with_api_key):
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": -5, "depth": 12}, "seat_count": 200},
        headers=_auth_header(),
    )
    assert r.status_code == 422  # pydantic gt=0 violation


def test_generate_400_on_zero_seats(client, with_api_key):
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 0},
        headers=_auth_header(),
    )
    assert r.status_code == 422


def test_generate_400_on_oversize_sketch(client, with_api_key):
    big = "A" * (ai_router.MAX_SKETCH_B64_CHARS + 100)
    r = client.post(
        "/api/ai/generate-layout",
        json={
            "venue_type": "theater",
            "dimensions": {"width": 15, "depth": 12},
            "seat_count": 200,
            "sketch_base64": big,
        },
        headers=_auth_header(),
    )
    assert r.status_code == 400
    assert "5 MB" in r.json()["detail"]


def test_generate_happy_path(client, with_api_key, stub_anthropic):
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200},
        headers=_auth_header(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == "2.0.0"
    assert body["venue"]["type"] == "theater"
    assert isinstance(body["seats"], list)


def test_generate_422_when_model_output_invalid(client, with_api_key, monkeypatch):
    def bad_model(system, user_content):
        return '{"schema_version": "1.0.0", "venue": {}}'
    monkeypatch.setattr(ai_router, "_call_anthropic", bad_model)
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200},
        headers=_auth_header(),
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "schema validation" in detail["message"].lower()
    assert detail["errors"]
    assert detail["raw_excerpt"]


def test_generate_422_when_model_returns_prose(client, with_api_key, monkeypatch):
    def prose_model(system, user_content):
        return "Sorry, I can't help with that today."
    monkeypatch.setattr(ai_router, "_call_anthropic", prose_model)
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200},
        headers=_auth_header(),
    )
    assert r.status_code == 422
    assert "could not be parsed" in r.json()["detail"]["message"].lower()


def test_generate_502_when_anthropic_raises(client, with_api_key, monkeypatch):
    def boom(system, user_content):
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail="AI provider error: TimeoutError")
    monkeypatch.setattr(ai_router, "_call_anthropic", boom)
    r = client.post(
        "/api/ai/generate-layout",
        json={"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200},
        headers=_auth_header(),
    )
    assert r.status_code == 502


def test_generate_rate_limit_after_10(client, with_api_key, stub_anthropic):
    payload = {"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200}
    # 10 successful requests
    for i in range(10):
        r = client.post("/api/ai/generate-layout", json=payload, headers=_auth_header())
        assert r.status_code == 200, f"request {i+1} failed: {r.text}"
    # 11th is rate-limited
    r = client.post("/api/ai/generate-layout", json=payload, headers=_auth_header())
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) > 0


def test_rate_limit_is_per_user(client, with_api_key, stub_anthropic):
    payload = {"venue_type": "theater", "dimensions": {"width": 15, "depth": 12}, "seat_count": 200}
    # User A uses up their bucket
    for _ in range(10):
        r = client.post("/api/ai/generate-layout", json=payload, headers=_auth_header(sub="test|user-A"))
        assert r.status_code == 200
    # User B is still allowed
    r = client.post("/api/ai/generate-layout", json=payload, headers=_auth_header(sub="test|user-B"))
    assert r.status_code == 200


def test_refine_happy_path(client, with_api_key, stub_anthropic):
    r = client.post(
        "/api/ai/refine-layout",
        json={"current_layout": _valid_layout(), "refinement_prompt": "make the center aisle wider"},
        headers=_auth_header(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == "2.0.0"


def test_refine_400_on_empty_layout(client, with_api_key, stub_anthropic):
    r = client.post(
        "/api/ai/refine-layout",
        json={"current_layout": {}, "refinement_prompt": "wider aisle"},
        headers=_auth_header(),
    )
    assert r.status_code == 400


def test_refine_503_when_api_key_missing(client, without_api_key, stub_anthropic):
    r = client.post(
        "/api/ai/refine-layout",
        json={"current_layout": _valid_layout(), "refinement_prompt": "wider aisle"},
        headers=_auth_header(),
    )
    assert r.status_code == 503


def test_other_endpoints_still_work_without_api_key(client, without_api_key):
    """AI being unavailable must not knock out the rest of the API."""
    r = client.get("/health")
    assert r.status_code == 200
    r = client.get("/events")
    assert r.status_code == 200


# ── Validator unit tests (bonus) ────────────────────────────
def test_validator_accepts_valid_layout():
    from app.ai_validator import validate_layout_v2
    assert validate_layout_v2(_valid_layout()) == []


def test_validator_rejects_wrong_schema_version():
    from app.ai_validator import validate_layout_v2
    L = _valid_layout()
    L["schema_version"] = "1.0.0"
    errs = validate_layout_v2(L)
    assert any("schema_version" in e for e in errs)


def test_validator_rejects_bad_uuid():
    from app.ai_validator import validate_layout_v2
    L = _valid_layout()
    L["seats"][0]["id"] = "not-a-uuid"
    errs = validate_layout_v2(L)
    assert any("seats[0].id" in e for e in errs)
