"""Who is calling, and may they (Phase 1c).

TWO CREDENTIALS, ONE HEADER
---------------------------
Everything under /v1 authenticates with `Authorization: Bearer <credential>`,
and the credential is one of two things:

    eyJhbGci...        an Auth0 access token - a HUMAN, acting through the
                       dashboard or the marketplace
    ksk_live_xxxx      an API key - a MACHINE, acting for one organisation

They are told apart by prefix, not by trying both: an Auth0 JWT is three
base64url segments and can never begin with `ksk_`. One header keeps clients
simple and keeps a single OpenAPI security scheme.

AUTHORISATION IS ALWAYS ORGANISATION-SCOPED
-------------------------------------------
Every /v1 resource belongs to exactly one organisation, so authorisation is the
same two questions everywhere:

    1. which organisation does THIS request act on?
    2. is this caller allowed to do THIS to that organisation?

(1) is the `resolve` callback on `Access`: for `/v1/orgs/{org_id}/...` it is the
path segment; for `/v1/performances/{id}/...`, `/v1/orders/{id}` and
`/v1/tickets/{id}` it is a lookup through the resource's owner. A resource whose
organisation does not match the caller's is reported as 404, never 403 - a 403
would confirm that another tenant's order id exists.

(2) differs by credential kind, which is why `Access` carries both knobs:

    roles=   membership roles a HUMAN must hold (spec 1 vocabulary)
    scope=   'read' or 'write', which a MACHINE's key must carry

`accept=` then declares which credential kinds the endpoint takes at all. An
endpoint that omits `scope` does not accept API keys; one that omits `roles`
does not accept user tokens. Both are stated per route rather than inherited, so
reading a route tells you its whole auth story - and `Access.describe()` renders
that same declaration into the endpoint's OpenAPI description, so the published
docs cannot drift from the code that enforces them.

WHY MEMBERSHIP AND NOT `users.role`
-----------------------------------
The legacy `users.role` is a single global string ('customer'/'organizer').
Decision 1 replaced it with per-organisation memberships, and this module is
where that becomes true for the API: `users.role` is never consulted here. The
legacy routers keep reading it, untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth as legacy_auth
from app.api import keys
from app.api.errors import Forbidden, Unauthenticated
from app.database import get_db
from app.engine_models import (
    ApiKey,
    EngineEvent,
    Membership,
    Order,
    Organization,
    Performance,
    Ticket,
)
from app.engine_services.errors import NotFound
from app.models import User

# ── Role tiers (spec 1 vocabulary) ──────────────────────────────────────────
#: Every role that exists. Used by read endpoints: any active member of an
#: organisation may look at that organisation.
ANY_MEMBER: tuple[str, ...] = (
    "owner",
    "admin",
    "event_manager",
    "venue_manager",
    "box_office",
    "finance",
    "support",
    "scanner",
    "marketing",
)
#: Organisation settings and credentials. Deliberately the narrowest tier:
#: minting an API key is minting the ability to sell.
ORG_ADMIN: tuple[str, ...] = ("owner", "admin")
#: Venues, layouts and layout versions - the seating estate.
VENUE_WRITE: tuple[str, ...] = ("owner", "admin", "venue_manager")
#: Events, performances and publishing.
EVENT_WRITE: tuple[str, ...] = ("owner", "admin", "event_manager")
#: Taking money for seats: the marketplace, the API and the box office.
SALES: tuple[str, ...] = ("owner", "admin", "event_manager", "box_office")
#: Reissuing a ticket's QR - support does this when a customer loses one.
TICKET_ADMIN: tuple[str, ...] = ("owner", "admin", "box_office", "support")
#: Reversing a sale. `finance` exists precisely for this and holds nothing else.
TICKET_REVERSE: tuple[str, ...] = ("owner", "admin", "box_office", "finance")
#: The door. `scanner` is a single-purpose role and this is its only tier.
SCAN: tuple[str, ...] = ("owner", "admin", "box_office", "scanner")

# ── Accepted credential kinds ───────────────────────────────────────────────
USER = "user"
API_KEY = "api_key"
ANONYMOUS = "anonymous"
BOTH: tuple[str, ...] = (USER, API_KEY)
USER_ONLY: tuple[str, ...] = (USER,)
API_KEY_ONLY: tuple[str, ...] = (API_KEY,)

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description=(
        "Either an Auth0 access token (a user acting through the dashboard) or "
        "an API key (`ksk_live_...` for the production environment, "
        "`ksk_test_...` for sandbox). Each endpoint documents which it accepts."
    ),
)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, already bound to the organisation it acts on."""

    kind: str
    organization: Organization
    user: User | None = None
    api_key: ApiKey | None = None
    role: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def organization_id(self) -> int:
        return self.organization.id

    @property
    def actor_user_id(self) -> int | None:
        """Threaded into every service call so `engine_audit_log` names the actor."""
        return self.user.id if self.user is not None else None

    @property
    def actor_api_key_id(self) -> int | None:
        return self.api_key.id if self.api_key is not None else None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# ── Organisation context for a user ─────────────────────────────────────────
def get_org_context(
    session: Session, user: User, organization_id: int
) -> tuple[Organization, Membership]:
    """The organisation and the user's ACTIVE membership of it.

    'invited' and 'disabled' memberships authorise nothing: an invitation that
    has not been accepted is not access, and disabling a member has to take
    effect without deleting the row recording that they were once there.

    Raises `NotFound` when the organisation does not exist OR the user is not a
    member of it - the two are deliberately indistinguishable to the caller, so
    organisation ids cannot be probed.
    """
    membership = session.execute(
        select(Membership).where(
            Membership.organization_id == organization_id,
            Membership.user_id == user.id,
            Membership.status == "active",
        )
    ).scalar_one_or_none()
    organization = session.get(Organization, organization_id)
    if membership is None or organization is None:
        raise NotFound(
            f"organization {organization_id} does not exist or you are not a "
            f"member of it",
            organization_id=organization_id,
        )
    if organization.status != "active":
        raise Forbidden(
            f"organization {organization_id} is {organization.status}",
            code="organization_suspended",
            detail={"organization_id": organization_id, "status": organization.status},
        )
    return organization, membership


def require(membership_role: str, *roles: str) -> None:
    """Role-tier gate. Raises 403 naming what would have been enough."""
    if membership_role not in roles:
        raise Forbidden(
            f"your role {membership_role!r} may not perform this action",
            code="insufficient_role",
            detail={"role": membership_role, "required_any_of": list(roles)},
        )


# ── Organisation resolvers: "which org does this request act on?" ────────────
def _path_int(params: dict[str, Any], name: str) -> int:
    return int(params[name])


def org_from_path(session: Session, params: dict[str, Any]) -> int:
    return _path_int(params, "org_id")


def org_from_performance(session: Session, params: dict[str, Any]) -> int:
    performance_id = _path_int(params, "performance_id")
    owner = session.execute(
        select(EngineEvent.organization_id)
        .join(Performance, Performance.event_id == EngineEvent.id)
        .where(Performance.id == performance_id)
    ).scalar_one_or_none()
    if owner is None:
        raise NotFound(f"performance {performance_id} does not exist")
    return int(owner)


def org_from_order(session: Session, params: dict[str, Any]) -> int:
    order_id = _path_int(params, "order_id")
    owner = session.execute(
        select(Order.organization_id).where(Order.id == order_id)
    ).scalar_one_or_none()
    if owner is None:
        raise NotFound(f"order {order_id} does not exist")
    return int(owner)


def org_from_ticket(session: Session, params: dict[str, Any]) -> int:
    ticket_id = _path_int(params, "ticket_id")
    owner = session.execute(
        select(Ticket.organization_id).where(Ticket.id == ticket_id)
    ).scalar_one_or_none()
    if owner is None:
        raise NotFound(f"ticket {ticket_id} does not exist")
    return int(owner)


# ── The dependency ──────────────────────────────────────────────────────────
class Access:
    """A per-endpoint declaration of who may call it, usable as a dependency::

        Depends(Access(org_from_path, roles=ORG_ADMIN, accept=USER_ONLY))

    Instances are callables, so FastAPI treats each as its own dependency and
    resolves it once per request.
    """

    def __init__(
        self,
        resolve: Callable[[Session, dict[str, Any]], int],
        *,
        roles: Sequence[str] | None = None,
        scope: str | None = None,
        accept: Sequence[str] = BOTH,
        anonymous: Callable[[Session, int, dict[str, Any]], bool] | None = None,
    ) -> None:
        self.resolve = resolve
        self.roles = tuple(roles or ())
        self.scope = scope
        self.accept = tuple(accept)
        self.anonymous = anonymous
        # Caught at import time rather than at the first request: an endpoint
        # that accepts a credential kind it cannot actually check would be an
        # authorisation hole, and this makes that combination unwritable.
        if USER in self.accept and not self.roles:
            raise ValueError("an endpoint accepting user tokens must declare roles")
        if API_KEY in self.accept and not self.scope:
            raise ValueError("an endpoint accepting API keys must declare a scope")

    def describe(self) -> str:
        """The `Auth:` line this endpoint's OpenAPI description carries."""
        parts: list[str] = []
        if USER in self.accept:
            who = (
                "any active member"
                if set(self.roles) == set(ANY_MEMBER)
                else ", ".join(self.roles)
            )
            parts.append(f"user token ({who})")
        if API_KEY in self.accept:
            parts.append(f"API key with `{self.scope}` scope")
        if self.anonymous is not None:
            parts.append("or no credential at all, for a publicly visible performance")
        return " · ".join(parts)

    async def __call__(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        db: Session = Depends(get_db),
    ) -> Principal:
        params = dict(request.path_params)

        if credentials is None or not credentials.credentials.strip():
            return self._anonymous(db, params)

        token = credentials.credentials.strip()
        if keys.looks_like_api_key(token):
            return self._as_api_key(db, params, token)
        return self._as_user(db, params, token)

    # ── credential kinds ────────────────────────────────────────────────
    def _anonymous(self, db: Session, params: dict[str, Any]) -> Principal:
        if self.anonymous is None:
            raise Unauthenticated(
                "this endpoint requires an Authorization: Bearer credential"
            )
        try:
            organization_id = self.resolve(db, params)
        except NotFound:
            # A stranger gets the same answer for "no such performance" as for
            # "that performance is not public yet". Letting the 404 through
            # would turn this endpoint into an existence oracle for every
            # organisation's unannounced shows.
            raise Unauthenticated(
                "this resource is not publicly readable; present a user token "
                "or an API key"
            ) from None
        if not self.anonymous(db, organization_id, params):
            raise Unauthenticated(
                "this resource is not publicly readable; present a user token "
                "or an API key"
            )
        organization = db.get(Organization, organization_id)
        return Principal(kind=ANONYMOUS, organization=organization)

    def _as_api_key(self, db: Session, params: dict[str, Any], token: str) -> Principal:
        if API_KEY not in self.accept:
            raise Forbidden(
                "this endpoint cannot be called with an API key; it requires a "
                "user token",
                code="credential_kind_not_accepted",
                detail={"presented": API_KEY, "accepted": list(self.accept)},
            )
        api_key = keys.resolve(db, token)
        if api_key is None:
            # One message for unknown / revoked / tampered, so a caller cannot
            # learn which of those their key is.
            raise Unauthenticated("the API key is not valid")

        organization_id = self.resolve(db, params)
        if api_key.organization_id != organization_id:
            raise NotFound("no such resource for this API key's organization")

        if self.scope and self.scope not in (api_key.scopes or []):
            raise Forbidden(
                f"this API key does not carry the {self.scope!r} scope",
                code="insufficient_scope",
                detail={"scopes": list(api_key.scopes or []), "required": self.scope},
            )

        organization = db.get(Organization, organization_id)
        if organization is None or organization.status != "active":
            raise Forbidden(
                "this organization is not active",
                code="organization_suspended",
                detail={"organization_id": organization_id},
            )

        keys.touch_last_used(db, api_key)
        return Principal(
            kind=API_KEY,
            organization=organization,
            api_key=api_key,
            scopes=tuple(api_key.scopes or ()),
        )

    def _as_user(self, db: Session, params: dict[str, Any], token: str) -> Principal:
        if USER not in self.accept:
            raise Forbidden(
                "this endpoint cannot be called with a user token; it requires "
                "an API key",
                code="credential_kind_not_accepted",
                detail={"presented": USER, "accepted": list(self.accept)},
            )
        user = current_user(db, token)
        organization_id = self.resolve(db, params)
        organization, membership = get_org_context(db, user, organization_id)
        require(membership.role, *self.roles)
        return Principal(
            kind=USER,
            organization=organization,
            user=user,
            role=membership.role,
        )


def current_user(db: Session, token: str) -> User:
    """Reuse the legacy JWT dependency's body, not its FastAPI signature.

    `app.auth.get_current_user` verifies the token, upserts the `users` row and
    provisions a personal organisation on first sight - all of which /v1 wants
    unchanged. It is declared as a FastAPI dependency, though, and /v1 needs to
    call it at a point where the organisation is not yet known, so its arguments
    are supplied by hand here. The alternative - a second copy of "who is this
    token" - is the thing worth avoiding.
    """
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials as _Creds

    try:
        return legacy_auth.get_current_user(
            creds=_Creds(scheme="Bearer", credentials=token), db=db
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            raise Unauthenticated(
                exc.detail if isinstance(exc.detail, str) else "invalid token"
            ) from None
        raise


@dataclass(frozen=True)
class Credential:
    """An authenticated caller BEFORE any organisation is known.

    Almost every endpoint learns its organisation from the path and so never
    needs this. `POST /v1/checkin` does: the only identifier in that request is
    the QR payload, and the organisation it belongs to is not known until the
    ticket has been resolved. So authentication and authorisation come apart
    there, and `bind_organization` is the second half.
    """

    kind: str
    user: User | None = None
    api_key: ApiKey | None = None


async def any_credential(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Credential:
    """Verify a user token or an API key without binding it to an organisation."""
    if credentials is None or not credentials.credentials.strip():
        raise Unauthenticated(
            "this endpoint requires an Authorization: Bearer credential"
        )
    token = credentials.credentials.strip()
    if keys.looks_like_api_key(token):
        api_key = keys.resolve(db, token)
        if api_key is None:
            raise Unauthenticated("the API key is not valid")
        keys.touch_last_used(db, api_key)
        return Credential(kind=API_KEY, api_key=api_key)
    return Credential(kind=USER, user=current_user(db, token))


def bind_organization(
    session: Session,
    credential: Credential,
    organization_id: int,
    *,
    roles: Sequence[str],
    scope: str,
) -> Principal | None:
    """Turn a `Credential` into a `Principal` for one organisation, or None.

    Returns None - rather than raising - when the caller has no business with
    this organisation at all. The check-in endpoint needs that distinction: a
    scanner presenting another organisation's ticket must be told `invalid`, not
    handed a 403 that confirms the ticket exists.
    """
    organization = session.get(Organization, organization_id)
    if organization is None or organization.status != "active":
        return None

    if credential.kind == API_KEY:
        api_key = credential.api_key
        if api_key.organization_id != organization_id:
            return None
        if scope not in (api_key.scopes or []):
            raise Forbidden(
                f"this API key does not carry the {scope!r} scope",
                code="insufficient_scope",
                detail={"scopes": list(api_key.scopes or []), "required": scope},
            )
        return Principal(
            kind=API_KEY,
            organization=organization,
            api_key=api_key,
            scopes=tuple(api_key.scopes or ()),
        )

    membership = session.execute(
        select(Membership).where(
            Membership.organization_id == organization_id,
            Membership.user_id == credential.user.id,
            Membership.status == "active",
        )
    ).scalar_one_or_none()
    if membership is None:
        return None
    require(membership.role, *roles)
    return Principal(
        kind=USER,
        organization=organization,
        user=credential.user,
        role=membership.role,
    )


async def authenticated_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """A user token and nothing else - for `/v1/me`, which precedes any
    organisation choice and so has no organisation to scope against."""
    if credentials is None or not credentials.credentials.strip():
        raise Unauthenticated(
            "this endpoint requires an Authorization: Bearer user token"
        )
    token = credentials.credentials.strip()
    if keys.looks_like_api_key(token):
        raise Forbidden(
            "this endpoint cannot be called with an API key; it describes a "
            "user, and an API key does not belong to one",
            code="credential_kind_not_accepted",
            detail={"presented": API_KEY, "accepted": [USER]},
        )
    return current_user(db, token)
