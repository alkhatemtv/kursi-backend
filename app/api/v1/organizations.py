"""Organizations and their API keys."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import keys
from app.api.auth import (
    ANY_MEMBER,
    ORG_ADMIN,
    USER_ONLY,
    Access,
    Principal,
    org_from_path,
)
from app.api.errors import ApiError
from app.api.pagination import Page, Paginated, page_params, paginate
from app.api.keys import SCOPE_READ, SCOPE_WRITE
from app.api.v1.schemas import (
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyOut,
    OrganizationOut,
    OrganizationPatch,
)
from app.database import get_db
from app.engine_models import ApiKey
from app.engine_services.audit import record_audit
from app.engine_services.errors import NotFound
from app.engine_services.uow import unit_of_work

router = APIRouter(prefix="/orgs", tags=["organizations"])

_READ = Access(org_from_path, roles=ANY_MEMBER, scope=SCOPE_READ)
_ADMIN = Access(org_from_path, roles=ORG_ADMIN, scope=SCOPE_WRITE)
#: Key management is a human action. A key that could mint further keys would
#: turn one leaked credential into permanent, self-renewing access.
_KEY_ADMIN = Access(org_from_path, roles=ORG_ADMIN, accept=USER_ONLY)

ACTION_KEY_CREATED = "api_key.created"
ACTION_KEY_REVOKED = "api_key.revoked"


@router.get(
    "/{org_id}",
    response_model=OrganizationOut,
    summary="Read an organization",
    description="**Auth:** " + _READ.describe(),
)
def read_organization(
    org_id: int = Path(description="Organization id, from `GET /v1/me`."),
    principal: Principal = Depends(_READ),
) -> OrganizationOut:
    return OrganizationOut.model_validate(principal.organization)


@router.patch(
    "/{org_id}",
    response_model=OrganizationOut,
    summary="Update an organization",
    description=(
        "Only the fields present in the body are changed. `branding` is stored "
        "and returned verbatim.\n\n"
        "`slug`, `type`, `plan` and `status` are not editable here: the first "
        "is a public identifier others may have linked to, and the rest are "
        "billing and lifecycle state that this API does not own.\n\n"
        "**Auth:** " + _ADMIN.describe()
    ),
)
def update_organization(
    body: OrganizationPatch,
    org_id: int = Path(...),
    principal: Principal = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationOut:
    organization = principal.organization
    changes = body.model_dump(exclude_unset=True)
    with unit_of_work(db):
        for field, value in changes.items():
            setattr(organization, field, value)
        if changes:
            record_audit(
                db,
                organization_id=organization.id,
                action="organization.updated",
                entity_type="organization",
                entity_id=organization.id,
                actor_user_id=principal.actor_user_id,
                actor_api_key_id=principal.actor_api_key_id,
                data={"fields": sorted(changes)},
            )
    db.refresh(organization)
    return OrganizationOut.model_validate(organization)


# ── API keys ────────────────────────────────────────────────────────────────
@router.get(
    "/{org_id}/api-keys",
    response_model=Paginated[ApiKeyOut],
    tags=["api-keys"],
    summary="List API keys",
    description=(
        "Metadata only. The key itself is not stored, so it cannot be listed - "
        "`key_prefix` is the visible handle you match against your secret "
        "store. Revoked keys are included so an audit can see them; a revoked "
        "key has `revoked_at` set and authenticates nothing.\n\n"
        "**Auth:** " + _KEY_ADMIN.describe()
    ),
)
def list_api_keys(
    org_id: int = Path(...),
    principal: Principal = Depends(_KEY_ADMIN),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[ApiKeyOut]:
    statement = (
        select(ApiKey)
        .where(ApiKey.organization_id == principal.organization_id)
        .order_by(ApiKey.id.desc())
    )
    rows, total = paginate(db, statement, page, count_over=ApiKey.id)
    return Paginated[ApiKeyOut](
        items=[ApiKeyOut.model_validate(row) for row in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/{org_id}/api-keys",
    response_model=ApiKeyCreatedOut,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
    summary="Create an API key",
    description=(
        "**The `key` field of this response is the only time the key exists in "
        "readable form.** Only a SHA-256 hash is stored, so it cannot be shown "
        "again, recovered, or read out of a database backup. Copy it into your "
        "secret store now; if you lose it, revoke it and create another.\n\n"
        "The prefix tells you which world a key acts on: `ksk_live_…` for the "
        "`production` environment, `ksk_test_…` for `sandbox`. Present it as "
        "`Authorization: Bearer ksk_live_…`.\n\n"
        "Scopes are coarse in this version: `read` for the seat map and "
        "read-only resources, `write` for everything that changes state. "
        "Requesting `write` stores `[\"read\", \"write\"]`, because a key that "
        "can sell can obviously also look.\n\n"
        "**Auth:** " + _KEY_ADMIN.describe()
    ),
)
def create_api_key(
    body: ApiKeyCreate,
    org_id: int = Path(...),
    principal: Principal = Depends(_KEY_ADMIN),
    db: Session = Depends(get_db),
) -> ApiKeyCreatedOut:
    try:
        scopes = keys.normalize_scopes(list(body.scopes))
    except ValueError as exc:
        raise ApiError(str(exc), code="invalid_request", http_status=422) from None

    token, key_prefix, key_hash = keys.mint(body.environment)

    with unit_of_work(db):
        row = ApiKey(
            organization_id=principal.organization_id,
            name=body.name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            environment=body.environment,
            scopes=scopes,
        )
        db.add(row)
        db.flush()
        record_audit(
            db,
            organization_id=principal.organization_id,
            action=ACTION_KEY_CREATED,
            entity_type="api_key",
            entity_id=row.id,
            actor_user_id=principal.actor_user_id,
            data={
                "name": body.name,
                "environment": body.environment,
                "scopes": scopes,
                # The prefix, never the key or its hash.
                "key_prefix": key_prefix,
            },
        )

    db.refresh(row)
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(row).model_dump(), key=token)


@router.delete(
    "/{org_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api-keys"],
    summary="Revoke an API key",
    description=(
        "Takes effect on the next request that presents the key - there is no "
        "cache to wait out. The row is kept rather than deleted so the audit "
        "trail still names the key that took past actions. Revoking an "
        "already-revoked key succeeds and changes nothing.\n\n"
        "**Auth:** " + _KEY_ADMIN.describe()
    ),
)
def revoke_api_key(
    org_id: int = Path(...),
    key_id: int = Path(description="Key id from `GET /v1/orgs/{org_id}/api-keys`."),
    principal: Principal = Depends(_KEY_ADMIN),
    db: Session = Depends(get_db),
) -> Response:
    row = db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id, ApiKey.organization_id == principal.organization_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"api key {key_id} does not exist")

    if row.revoked_at is None:
        with unit_of_work(db):
            row.revoked_at = datetime.now(timezone.utc)
            record_audit(
                db,
                organization_id=principal.organization_id,
                action=ACTION_KEY_REVOKED,
                entity_type="api_key",
                entity_id=row.id,
                actor_user_id=principal.actor_user_id,
                data={"key_prefix": row.key_prefix, "name": row.name},
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
