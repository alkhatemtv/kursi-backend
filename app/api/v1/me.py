"""`GET /v1/me` - the entry point every dashboard session starts from."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import authenticated_user
from app.api.v1.schemas import MembershipOut, MeOut
from app.database import get_db
from app.engine_models import Membership, Organization
from app.models import User

router = APIRouter(tags=["identity"])


@router.get(
    "/me",
    response_model=MeOut,
    summary="The authenticated user and their organizations",
    description=(
        "Returns the caller and every organization they belong to, with their "
        "role in each. Call this first: every other endpoint is scoped to one "
        "organization, and this is where a client learns which ids it may use.\n\n"
        "A user who belongs to nothing still gets one membership back. A personal "
        "organization is provisioned on first authenticated request, so the list "
        "is never empty.\n\n"
        "**Auth:** user token only. An API key already belongs to exactly one "
        "organization and to no user, so this endpoint has nothing to tell it."
    ),
)
def me(
    user: User = Depends(authenticated_user),
    db: Session = Depends(get_db),
) -> MeOut:
    rows = db.execute(
        select(Membership, Organization)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(Membership.user_id == user.id)
        .order_by(Membership.id)
    ).all()

    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        memberships=[
            MembershipOut(
                organization_id=org.id,
                organization_name=org.name,
                organization_slug=org.slug,
                organization_type=org.type,
                organization_plan=org.plan,
                role=membership.role,
                status=membership.status,
            )
            for membership, org in rows
        ],
    )
