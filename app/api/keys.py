"""API keys: minting, storage, lookup (spec 6, `engine_api_keys`).

THE KEY IS SHOWN ONCE AND STORED AS A HASH
------------------------------------------
`engine_api_keys` has `key_prefix` (visible) and `key_hash` - deliberately no
column for the key itself. A database leak therefore yields no working
credential. The consequence the API has to live with is that a lost key cannot
be recovered, only replaced; `POST /v1/orgs/{id}/api-keys` says so in its
response and its OpenAPI description.

WHY sha256 AND NOT bcrypt/argon2
--------------------------------
Password hashes are slow on purpose because humans choose low-entropy secrets
that can be guessed. An API key here is 32 characters from `secrets.token_urlsafe`
- around 190 bits - so there is nothing to guess: an attacker who can brute-force
that can brute-force the bcrypt too. What matters instead is that verification is
cheap enough to run on EVERY request without becoming its own bottleneck, and
that comparison is constant-time. A key-stretching KDF here would buy nothing and
cost ~100 ms per API call.

LOOKUP IS INDEXED, COMPARISON IS CONSTANT-TIME
----------------------------------------------
Scanning every key row and hashing against each would be O(keys) per request.
Instead the first 8 characters of the secret are stored alongside the
environment prefix in the indexed `key_prefix` column, so presentation of a key
resolves to (almost always) a single row by index. That row's `key_hash` is then
compared with `hmac.compare_digest`, so a timing signal cannot walk the hash out
of us. The prefix is not a secret and is safe to display in a dashboard.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.engine_models import ApiKey

#: `api_keys.environment` is CHECK-constrained to these two (Phase 1a schema).
#: The visible prefix is what a client sees and what tells a support engineer at
#: a glance which world a key belongs to.
KEY_PREFIXES: dict[str, str] = {"production": "ksk_live_", "sandbox": "ksk_test_"}
ENVIRONMENT_BY_PREFIX: dict[str, str] = {v: k for k, v in KEY_PREFIXES.items()}

#: Coarse scopes for Phase 1c. `write` implies `read`; finer per-resource scopes
#: are a Phase 4 developer-portal concern and would need no schema change
#: (`api_keys.scopes` is already TEXT[]).
SCOPE_READ = "read"
SCOPE_WRITE = "write"
VALID_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE)

_SECRET_CHARS = 32
_PREFIX_SAMPLE = 8

#: How stale `last_used_at` may get before a request bothers to refresh it.
#: See `touch_last_used`.
LAST_USED_RESOLUTION = timedelta(seconds=60)


def looks_like_api_key(credential: str) -> bool:
    """Is this bearer credential one of ours, rather than an Auth0 JWT?

    Cheap and total: JWTs are three base64url segments and can never begin with
    `ksk_`, so the prefix alone decides which verifier gets the string. Nothing
    downstream has to try both.
    """
    return credential.startswith("ksk_")


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def environment_of(token: str) -> str | None:
    for prefix, environment in ENVIRONMENT_BY_PREFIX.items():
        if token.startswith(prefix):
            return environment
    return None


def prefix_of(token: str) -> str | None:
    """The indexed lookup handle: `ksk_live_` + the first 8 secret characters."""
    for prefix in ENVIRONMENT_BY_PREFIX:
        if token.startswith(prefix):
            return prefix + token[len(prefix) : len(prefix) + _PREFIX_SAMPLE]
    return None


def mint(environment: str) -> tuple[str, str, str]:
    """(token, key_prefix, key_hash). The token is never persisted."""
    prefix = KEY_PREFIXES[environment]
    secret = secrets.token_urlsafe(_SECRET_CHARS)[:_SECRET_CHARS]
    token = f"{prefix}{secret}"
    return token, f"{prefix}{secret[:_PREFIX_SAMPLE]}", hash_key(token)


def normalize_scopes(scopes: list[str] | None) -> list[str]:
    """Deduplicate, order stably, and imply `read` from `write`.

    Storing the implication rather than computing it at check time means a
    dashboard shows a write key as `["read", "write"]`, which is what it can
    actually do - no reader has to know the rule.
    """
    requested = {str(s).strip().lower() for s in (scopes or [SCOPE_READ]) if str(s).strip()}
    unknown = sorted(requested - set(VALID_SCOPES))
    if unknown:
        raise ValueError(
            f"unknown scope(s): {', '.join(unknown)}; expected "
            f"{', '.join(VALID_SCOPES)}"
        )
    if SCOPE_WRITE in requested:
        requested.add(SCOPE_READ)
    return [s for s in VALID_SCOPES if s in requested]


def resolve(session: Session, token: str) -> ApiKey | None:
    """The presented key's live row, or None.

    None covers every failure the caller must not be able to tell apart: no such
    key, wrong hash, revoked, or an environment that disagrees with the prefix.
    """
    prefix = prefix_of(token)
    environment = environment_of(token)
    if prefix is None or environment is None:
        return None

    presented = hash_key(token)
    candidates = (
        session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
        .scalars()
        .all()
    )
    for row in candidates:
        if not hmac.compare_digest(row.key_hash, presented):
            continue
        if row.revoked_at is not None or row.environment != environment:
            return None
        return row
    return None


def touch_last_used(session: Session, api_key: ApiKey) -> None:
    """Record that the key was used, at most once per `LAST_USED_RESOLUTION`.

    WHY NOT AN UNCONDITIONAL UPDATE
    -------------------------------
    A busy key would then take a row-level write lock on the SAME row on every
    single request, serialising that organisation's whole API behind one row.
    The `WHERE last_used_at < cutoff` clause means the usual request matches
    zero rows, takes no write lock at all, and commits nothing.

    This is telemetry, not correctness, so it reads the process wall clock
    rather than paying for a round-trip to the database clock, and a failure to
    record it is swallowed - a caller with a valid key must not be handed a 500
    because we could not update a timestamp.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - LAST_USED_RESOLUTION
    try:
        session.execute(
            update(ApiKey)
            .where(
                ApiKey.id == api_key.id,
                or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at < cutoff),
            )
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception:  # pragma: no cover - telemetry must never fail a request
        session.rollback()
