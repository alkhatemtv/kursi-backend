"""Who may call /v1, and what a refusal looks like (Phase 1c).

The matrix these tests walk, for every kind of endpoint:

    no credential          -> 401
    unverifiable token     -> 401
    valid token, other org -> 404   (never 403 - see below)
    valid token, low role  -> 403   with the role and the tiers that would do
    valid token, right role-> 200
    API key, read scope    -> 200 on reads, 403 on writes
    API key, write scope   -> 200
    API key, other org     -> 404
    revoked key            -> 401

WHY "OTHER ORG" IS 404 AND NOT 403
----------------------------------
403 would confirm that the resource exists. Given ids are sequential, a 403 on
`/v1/orders/91` and a 404 on `/v1/orders/92` tells an attacker exactly how many
orders the platform has and which ids are real. So a resource the caller has no
relationship with is indistinguishable from one that was never created.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import engine_models as em
from tests.engine.conftest import key_header, role_header, user_header

pytestmark = pytest.mark.usefixtures("api_world")


def org_url(world, suffix: str = "") -> str:
    return f"/v1/orgs/{world['org_id']}{suffix}"


def envelope(response) -> dict:
    """Assert the /v1 envelope and return the body."""
    body = response.json()
    assert "error" in body, f"missing error code in {body}"
    assert isinstance(body["error"], str)
    assert "message" in body and isinstance(body["message"], str)
    assert "detail" not in body or isinstance(body["detail"], dict)
    return body


class TestUserTokenMatrix:
    def test_no_credential_is_401_in_the_v1_envelope(self, api_client, world):
        response = api_client.get(org_url(world))
        assert response.status_code == 401
        assert envelope(response)["error"] == "unauthenticated"

    def test_unverifiable_token_is_401(self, api_client, world):
        response = api_client.get(
            org_url(world), headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401
        assert envelope(response)["error"] == "unauthenticated"

    def test_a_member_may_read_their_own_organization(self, api_client, world):
        response = api_client.get(org_url(world), headers=role_header("marketing"))
        assert response.status_code == 200
        assert response.json()["id"] == world["org_id"]

    def test_another_organizations_member_gets_404_not_403(self, api_client, world):
        response = api_client.get(
            org_url(world), headers=user_header("test|outsider")
        )
        assert response.status_code == 404
        assert envelope(response)["error"] == "not_found"

    def test_an_invited_membership_authorises_nothing(self, api_client, world):
        """Being invited is not being a member. It reads as 'not your org'."""
        response = api_client.get(org_url(world), headers=user_header("test|invited"))
        assert response.status_code == 404

    def test_insufficient_role_is_403_naming_what_would_have_worked(
        self, api_client, world
    ):
        response = api_client.patch(
            org_url(world), json={"name": "Renamed"}, headers=role_header("scanner")
        )
        assert response.status_code == 403
        body = envelope(response)
        assert body["error"] == "insufficient_role"
        assert body["detail"]["role"] == "scanner"
        assert set(body["detail"]["required_any_of"]) == {"owner", "admin"}

    @pytest.mark.parametrize("role", ["owner", "admin"])
    def test_the_admin_tier_may_write(self, api_client, world, role):
        response = api_client.patch(
            org_url(world), json={"name": "Renamed"}, headers=role_header(role)
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    def test_role_tiers_differ_per_resource(self, api_client, world):
        """venue_manager owns the seating estate but not organization settings."""
        allowed = api_client.post(
            org_url(world, "/venues"),
            json={"name": "Studio"},
            headers=role_header("venue_manager"),
        )
        assert allowed.status_code == 201

        refused = api_client.patch(
            org_url(world), json={"name": "Nope"}, headers=role_header("venue_manager")
        )
        assert refused.status_code == 403


class TestApiKeyMatrix:
    def test_a_read_key_may_read(self, api_client, world, make_api_key):
        token = make_api_key(world["org_id"], scopes=["read"])
        response = api_client.get(org_url(world), headers=key_header(token))
        assert response.status_code == 200

    def test_a_read_key_may_not_write(self, api_client, world, make_api_key):
        token = make_api_key(world["org_id"], scopes=["read"])
        response = api_client.post(
            org_url(world, "/venues"), json={"name": "Studio"}, headers=key_header(token)
        )
        assert response.status_code == 403
        body = envelope(response)
        assert body["error"] == "insufficient_scope"
        assert body["detail"]["required"] == "write"

    def test_a_write_key_may_write_and_implies_read(
        self, api_client, world, make_api_key
    ):
        token = make_api_key(world["org_id"], scopes=["write"])
        created = api_client.post(
            org_url(world, "/venues"), json={"name": "Studio"}, headers=key_header(token)
        )
        assert created.status_code == 201
        assert api_client.get(org_url(world), headers=key_header(token)).status_code == 200

    def test_another_organizations_key_gets_404(self, api_client, world, make_api_key):
        token = make_api_key(world["other_org_id"], scopes=["write"])
        response = api_client.get(org_url(world), headers=key_header(token))
        assert response.status_code == 404

    def test_a_revoked_key_authenticates_nothing(self, api_client, world, make_api_key):
        token = make_api_key(world["org_id"], scopes=["write"], revoked=True)
        response = api_client.get(org_url(world), headers=key_header(token))
        assert response.status_code == 401
        assert envelope(response)["error"] == "unauthenticated"

    def test_a_tampered_key_authenticates_nothing(
        self, api_client, world, make_api_key
    ):
        token = make_api_key(world["org_id"], scopes=["write"])
        response = api_client.get(org_url(world), headers=key_header(token[:-1] + "X"))
        assert response.status_code == 401

    def test_using_a_key_stamps_last_used_at(
        self, api_client, world, make_api_key, session
    ):
        token = make_api_key(world["org_id"], scopes=["read"])
        assert api_client.get(org_url(world), headers=key_header(token)).status_code == 200
        session.expire_all()
        row = session.execute(select(em.ApiKey)).scalars().one()
        assert row.last_used_at is not None


class TestCredentialKindIsDeclaredPerEndpoint:
    def test_me_refuses_an_api_key(self, api_client, world, make_api_key):
        token = make_api_key(world["org_id"], scopes=["write"])
        response = api_client.get("/v1/me", headers=key_header(token))
        assert response.status_code == 403
        assert envelope(response)["error"] == "credential_kind_not_accepted"

    def test_me_accepts_a_user_token(self, api_client, world):
        response = api_client.get("/v1/me", headers=role_header("owner"))
        assert response.status_code == 200
        body = response.json()
        assert [m["organization_id"] for m in body["memberships"]] == [world["org_id"]]
        assert body["memberships"][0]["role"] == "owner"

    def test_key_management_refuses_an_api_key(self, api_client, world, make_api_key):
        """A key that could mint keys would make one leak permanent."""
        token = make_api_key(world["org_id"], scopes=["write"])
        response = api_client.post(
            org_url(world, "/api-keys"),
            json={"name": "bootstrap"},
            headers=key_header(token),
        )
        assert response.status_code == 403
        assert envelope(response)["error"] == "credential_kind_not_accepted"


class TestKeyLifecycle:
    def test_create_returns_the_key_once_and_never_again(self, api_client, world):
        created = api_client.post(
            org_url(world, "/api-keys"),
            json={"name": "Box office iPad", "environment": "production",
                  "scopes": ["write"]},
            headers=role_header("owner"),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["key"].startswith("ksk_live_")
        assert body["key_prefix"] == body["key"][: len("ksk_live_") + 8]
        # `write` is stored with the `read` it implies.
        assert body["scopes"] == ["read", "write"]

        listed = api_client.get(org_url(world, "/api-keys"), headers=role_header("owner"))
        assert listed.status_code == 200
        item = listed.json()["items"][0]
        assert "key" not in item
        assert item["key_prefix"] == body["key_prefix"]

    def test_a_sandbox_key_gets_the_test_prefix(self, api_client, world):
        created = api_client.post(
            org_url(world, "/api-keys"),
            json={"name": "sandbox", "environment": "sandbox"},
            headers=role_header("owner"),
        )
        assert created.json()["key"].startswith("ksk_test_")

    def test_the_key_it_returns_actually_works(self, api_client, world):
        created = api_client.post(
            org_url(world, "/api-keys"),
            json={"name": "live", "environment": "production", "scopes": ["write"]},
            headers=role_header("owner"),
        )
        token = created.json()["key"]
        assert api_client.get(org_url(world), headers=key_header(token)).status_code == 200

    def test_revoking_takes_effect_on_the_next_request(self, api_client, world):
        created = api_client.post(
            org_url(world, "/api-keys"),
            json={"name": "short lived", "scopes": ["read"]},
            headers=role_header("owner"),
        ).json()
        token = created["key"]
        assert api_client.get(org_url(world), headers=key_header(token)).status_code == 200

        revoked = api_client.delete(
            org_url(world, f"/api-keys/{created['id']}"), headers=role_header("owner")
        )
        assert revoked.status_code == 204
        assert api_client.get(org_url(world), headers=key_header(token)).status_code == 401

    def test_revoking_twice_is_not_an_error(self, api_client, world):
        created = api_client.post(
            org_url(world, "/api-keys"),
            json={"name": "k"},
            headers=role_header("owner"),
        ).json()
        url = org_url(world, f"/api-keys/{created['id']}")
        assert api_client.delete(url, headers=role_header("owner")).status_code == 204
        assert api_client.delete(url, headers=role_header("owner")).status_code == 204

    def test_only_the_hash_is_persisted(self, api_client, world, session):
        created = api_client.post(
            org_url(world, "/api-keys"),
            json={"name": "k", "scopes": ["write"]},
            headers=role_header("owner"),
        ).json()
        row = session.execute(select(em.ApiKey)).scalars().one()
        assert created["key"] not in (row.key_hash, row.key_prefix)
        assert len(row.key_hash) == 64  # sha256 hex


class TestLegacyResponsesAreUnchanged:
    """The /v1 handlers are registered app-wide; these prove they keep their
    hands off everything else."""

    def test_a_legacy_auth_failure_still_uses_fastapis_detail_shape(self, api_client):
        response = api_client.get("/events/mine/list")
        assert response.status_code == 401
        assert set(response.json()) == {"detail"}

    def test_a_legacy_404_still_uses_fastapis_detail_shape(self, api_client):
        response = api_client.get("/events/999999")
        assert response.status_code == 404
        assert set(response.json()) == {"detail"}

    def test_a_legacy_validation_error_still_uses_fastapis_shape(self, api_client):
        response = api_client.get("/events?page=0")
        assert response.status_code == 422
        assert set(response.json()) == {"detail"}

    def test_health_is_untouched(self, api_client):
        response = api_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestPagination:
    def test_limit_is_capped(self, api_client, world):
        response = api_client.get(
            org_url(world, "/venues?limit=5000"), headers=role_header("owner")
        )
        assert response.status_code == 422
        assert envelope(response)["error"] == "invalid_request"

    def test_a_page_reports_the_whole_total(self, api_client, world):
        for i in range(3):
            api_client.post(
                org_url(world, "/venues"),
                json={"name": f"Hall {i}"},
                headers=role_header("owner"),
            )
        response = api_client.get(
            org_url(world, "/venues?limit=2&offset=0"), headers=role_header("owner")
        )
        body = response.json()
        assert len(body["items"]) == 2
        assert body["total"] == 4  # three created here, plus `world`'s venue
        assert (body["limit"], body["offset"]) == (2, 0)
