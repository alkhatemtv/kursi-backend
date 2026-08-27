"""Ticket credentials - a deliberate Phase 1b stub with the right SHAPE.

Decision 4 separates the ticket's identity from its credential: `tickets.id` is
stable for life, while the QR contains an opaque signed token that resolves to
`{ticket_id, credential_version}` and can be rotated without reissuing the
ticket.

Phase 1b needs that split to exist so that `complete_order` can write
`credential_version` and `credential_hash` and so rotation has something to
rotate. It does NOT need the final token format, which belongs with the scanner
API in Phase 1c. What is real here:

* the token is opaque to the holder (it carries no readable seat or order data);
* it is signed, so a forged token fails verification rather than resolving;
* only the HASH is stored, never the token, so a database leak does not mint
  working tickets;
* `verify` returns the version, which is what makes "superseded credential" a
  distinguishable scan verdict.

What is not final: the signature is HMAC-SHA256 over a dotted payload with no
expiry, no key id and no rotation of the signing key itself. Phase 1c replaces
the encoding; the interface below is what callers should be written against.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger("kursi.engine.credentials")

#: Used only when no secret is configured. Fine for development and tests,
#: refused in production - see `_signing_key`.
_DEV_SECRET = "kursi-engine-dev-credential-secret"

_SIG_BYTES = 24  # 192 bits of signature, base64url-encoded to 32 chars


def _signing_key() -> bytes:
    secret = (
        os.environ.get("ENGINE_CREDENTIAL_SECRET")
        or getattr(settings, "engine_credential_secret", "")
        or ""
    ).strip()
    if not secret:
        if settings.is_production:
            raise RuntimeError(
                "ENGINE_CREDENTIAL_SECRET is not set. Ticket credentials must not "
                "be signed with the development key in production."
            )
        secret = _DEV_SECRET
    return secret.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True)
class Credential:
    """`token` is handed to the ticket holder; `hash` is what the row stores."""

    token: str
    hash: str


def credential_hash(token: str) -> str:
    """SHA-256 of the token. Storing this, not the token, is the point."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_credential(ticket_id: int, credential_version: int = 1) -> Credential:
    payload = _b64(f"{ticket_id}.{credential_version}".encode("utf-8"))
    signature = _b64(
        hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).digest()[
            :_SIG_BYTES
        ]
    )
    token = f"kt1.{payload}.{signature}"
    return Credential(token=token, hash=credential_hash(token))


def verify_credential(token: str) -> tuple[int, int] | None:
    """(ticket_id, credential_version), or None if the token is not ours.

    Says nothing about whether the ticket is valid - that is the scanner's job
    in Phase 1c, which compares the version against the row and applies the
    verdict table in spec 5.
    """
    try:
        prefix, payload, signature = token.split(".", 2)
    except ValueError:
        return None
    if prefix != "kt1":
        return None

    expected = _b64(
        hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).digest()[
            :_SIG_BYTES
        ]
    )
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        ticket_id, version = _unb64(payload).decode("utf-8").split(".", 1)
        return int(ticket_id), int(version)
    except (ValueError, UnicodeDecodeError):  # pragma: no cover - malformed payload
        return None
