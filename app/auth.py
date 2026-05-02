"""Auth0 JWT verification + automatic user provisioning.

How this works:
  1. Frontend obtains an access token from Auth0 (via the SPA login flow).
  2. Frontend sends every API request with `Authorization: Bearer <token>`.
  3. We fetch Auth0's public keys (JWKS) once and cache them.
  4. We verify the JWT signature, expiration, audience, and issuer.
  5. We look up (or create) a local `User` row keyed by the Auth0 `sub` claim,
     so every authenticated request gives us a database user.
"""
from functools import lru_cache
from typing import Any

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWTError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User

bearer_scheme = HTTPBearer(auto_error=True)


@lru_cache(maxsize=1)
def _get_jwks() -> dict[str, Any]:
    """Fetch and cache Auth0 JSON Web Key Set.

    JWKS rarely rotates; caching for the process lifetime is fine for this scale.
    For production with key rotation, swap this for a TTL cache.
    """
    if not settings.auth0_domain:
        raise RuntimeError(
            "AUTH0_DOMAIN is not configured. Set it in your .env file."
        )
    url = f"https://{settings.auth0_domain}/.well-known/jwks.json"
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _decode_token(token: str) -> dict[str, Any]:
    """Verify a JWT against Auth0 and return its claims."""
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Malformed token: {e}",
        )

    jwks = _get_jwks()
    rsa_key: dict[str, str] | None = None
    for key in jwks.get("keys", []):
        if key.get("kid") == unverified_header.get("kid"):
            rsa_key = {
                "kty": key["kty"],
                "kid": key["kid"],
                "use": key["use"],
                "n": key["n"],
                "e": key["e"],
            }
            break

    if rsa_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to find matching JWK for token",
        )

    try:
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=settings.auth0_algorithms_list,
            audience=settings.auth0_api_audience,
            issuer=f"https://{settings.auth0_domain}/",
        )
        return payload
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired"
        )
    except JWTClaimsError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token claims: {e}",
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}"
        )


def _extract_email(claims: dict[str, Any]) -> str:
    """Email may live in a custom Auth0 claim namespace, the standard `email` claim,
    or nowhere at all. Fall back to a synthetic email so user creation never fails."""
    ns = settings.auth0_namespace
    return (
        claims.get(f"{ns}email")
        or claims.get("email")
        or f"{claims.get('sub', 'unknown').replace('|', '_')}@noemail.kursi.io"
    )


def _extract_role(claims: dict[str, Any]) -> str | None:
    """Role lives in a custom claim if you set up an Auth0 Action (see README).
    Returns None if not present so we don't overwrite a previously-set role."""
    ns = settings.auth0_namespace
    role = claims.get(f"{ns}role") or claims.get("role")
    if role in ("customer", "organizer"):
        return role
    return None


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: validates the bearer token and returns (or creates) the User."""
    claims = _decode_token(creds.credentials)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub claim"
        )

    user = db.query(User).filter(User.auth0_sub == sub).first()

    if user is None:
        # First-time login — provision a user record from the token claims
        user = User(
            auth0_sub=sub,
            email=_extract_email(claims),
            name=claims.get(f"{settings.auth0_namespace}name") or claims.get("name"),
            role=_extract_role(claims) or "customer",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Keep email + role in sync with the token, but don't downgrade to customer
        # if the user already has a higher role recorded.
        new_email = _extract_email(claims)
        new_role = _extract_role(claims)
        changed = False
        if new_email and user.email != new_email:
            user.email = new_email
            changed = True
        if new_role and user.role != new_role:
            user.role = new_role
            changed = True
        if changed:
            db.commit()
            db.refresh(user)

    return user


def require_organizer(user: User = Depends(get_current_user)) -> User:
    """Dependency for routes that only organizers may access."""
    if user.role != "organizer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organizer role required for this action",
        )
    return user
