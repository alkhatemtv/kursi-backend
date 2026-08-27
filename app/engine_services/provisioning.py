"""Personal-organization auto-provisioning (spec 1).

    "on first authenticated request, auto-create user (existing behavior) AND a
     personal organization + owner membership if the user has none."

WHY THE SLUG IS THE RACE ARBITER
--------------------------------
Two tabs, one cold user, two requests in flight: both find no membership and
both decide to create an organization. Exactly one may win. The candidates were:

* **PostgreSQL advisory lock** - works, but is Postgres-only, so the concurrency
  test would have to be gated to a backend that is not the default one. A race
  guard that cannot be tested on the day-to-day harness is a race guard nobody
  exercises.
* **SERIALIZABLE + retry** - correct, but it escalates the isolation level of
  the *authentication path* of a live marketplace to solve a problem that
  happens once per user, ever.
* **Insert-and-catch on a UNIQUE index** - chosen. Same philosophy as the seat
  locks: the database decides, application code just reacts. Portable to SQLite,
  so the concurrency test runs on the default harness.

For that to work the index has to be a *per-user* arbiter, which means the slug
must be derived deterministically from the user: two racers must generate the
SAME slug and collide. Hence `personal-org slug = <name-slug>-<user id>`. The
tempting alternative - try "ali", fall back to "ali-2" on collision - is exactly
wrong here: the loser would invent a new slug and successfully create a SECOND
organization for the same person.

There is no `UNIQUE(user_id) WHERE personal` constraint to lean on instead;
adding one would mean a new migration, and Phase 1b changes no schema.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.engine_models import Membership, Organization
from app.engine_services.audit import ACTION_ORG_PROVISIONED, record_audit
from app.engine_services.uow import unit_of_work
from app.models import User

logger = logging.getLogger("kursi.engine.provisioning")

#: Long enough to stay readable, short enough to stay a URL component.
_SLUG_MAX = 48
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str | None, fallback: str = "org") -> str:
    """Lowercase, ASCII-ish, hyphen-separated. Never empty."""
    cleaned = _SLUG_STRIP.sub("-", (value or "").strip().lower()).strip("-")
    cleaned = cleaned[:_SLUG_MAX].strip("-")
    return cleaned or fallback


def _display_name(user: User) -> str:
    """A human label for the personal org: the user's name, else their email
    local part, else something that is at least unambiguous."""
    if user.name and user.name.strip():
        return user.name.strip()
    if user.email and "@" in user.email:
        local = user.email.split("@", 1)[0].strip()
        if local:
            return local
    return f"User {user.id}"


def personal_org_slug(user: User) -> str:
    """Deterministic per user - this is what makes the UNIQUE index an arbiter.

    Appending the user id also settles the ordinary (non-race) collision the
    spec asks about: two different people called Ali get `ali-7` and `ali-12`.
    """
    return f"{slugify(_display_name(user), fallback='user')}-{user.id}"


def find_active_organization(session: Session, user: User) -> Organization | None:
    """The org behind the user's first ACTIVE membership, if any.

    'invited' and 'disabled' memberships deliberately do not count: a user who
    was invited somewhere but has not accepted still has no home of their own.
    """
    return session.execute(
        select(Organization)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(Membership.user_id == user.id, Membership.status == "active")
        .order_by(Membership.id)
        .limit(1)
    ).scalar_one_or_none()


def _ensure_owner_membership(
    session: Session, organization: Organization, user: User
) -> Membership:
    """Idempotent: UNIQUE(organization_id, user_id) is the arbiter again."""
    existing = session.execute(
        select(Membership).where(
            Membership.organization_id == organization.id,
            Membership.user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    membership = Membership(
        organization_id=organization.id,
        user_id=user.id,
        role="owner",
        status="active",
    )
    session.add(membership)
    session.flush()
    return membership


def ensure_personal_organization(
    session: Session, user: User
) -> tuple[Organization, bool]:
    """Return (organization, created). Idempotent, and safe under concurrency.

    The whole creation - organization, owner membership, audit row - is one
    transaction, so a user can never end up owning an organization they are not
    a member of.
    """
    existing = find_active_organization(session, user)
    if existing is not None:
        return existing, False

    slug = personal_org_slug(user)
    name = _display_name(user)

    try:
        with unit_of_work(session):
            organization = Organization(
                name=name,
                slug=slug,
                type="personal",
                plan="personal",
                status="active",
            )
            session.add(organization)
            session.flush()

            membership = _ensure_owner_membership(session, organization, user)

            record_audit(
                session,
                organization_id=organization.id,
                action=ACTION_ORG_PROVISIONED,
                entity_type="organization",
                entity_id=organization.id,
                actor_user_id=user.id,
                data={
                    "slug": slug,
                    "name": name,
                    "type": "personal",
                    "membership_id": membership.id,
                    "user_id": user.id,
                },
            )
        return organization, True

    except IntegrityError:
        # The other racer got there first. `session.rollback()` has already run
        # inside unit_of_work; re-read what they committed and adopt it. If the
        # row still is not there, the failure was NOT the race we are handling,
        # so let it out.
        session.rollback()
        organization = session.execute(
            select(Organization).where(Organization.slug == slug)
        ).scalar_one_or_none()
        if organization is None:
            raise
        logger.info(
            "personal org %s already provisioned concurrently for user %s",
            slug,
            user.id,
        )
        try:
            with unit_of_work(session):
                _ensure_owner_membership(session, organization, user)
        except IntegrityError:
            # UNIQUE(organization_id, user_id) fired: the winner's membership
            # landed between our SELECT and our INSERT. Nothing left to do.
            session.rollback()
        return organization, False
