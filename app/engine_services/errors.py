"""Structured errors for the Engine service layer (Phase 1b).

WHY STRUCTURED, NOT STRINGS
---------------------------
Every failure here is something a caller has to *act* on: a checkout UI must
repaint exactly the seats that were taken and say why, an API client must
distinguish "your hold expired" from "that seat is sold". A bare
``ValueError("seat taken")`` cannot carry that, so every error in this module
exposes:

    .code          a stable machine string (never localised, never reworded)
    .http_status   the status Phase 1c should map it to - a hint, not a
                   dependency; nothing here imports FastAPI
    .as_dict()     a JSON-safe body, ready to become a response payload

No route consumes these yet (routes are Phase 1c); the shapes are fixed now so
that the service layer and the tests agree on them before any HTTP exists.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class EngineServiceError(Exception):
    """Base class. `code` is the contract; the message is for humans/logs."""

    code = "engine_error"
    http_status = 400

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.code}: {self.message}>"


class EngineConflict(EngineServiceError):
    """The request was well-formed but lost to the current state of the world."""

    code = "conflict"
    http_status = 409


class NotFound(EngineServiceError):
    code = "not_found"
    http_status = 404


class ValidationError(EngineServiceError):
    code = "invalid_request"
    http_status = 422


# ── Seat availability ───────────────────────────────────────────────────────
#: Reason codes carried by SeatConflict. Exhaustive by construction: the
#: availability predicate has exactly three ways to say "no", plus "no such seat".
REASON_UNKNOWN_SEAT = "unknown_seat"      # seat_uid not in this performance's inventory
REASON_SEAT_STATUS = "seat_status"        # inventory status is not 'available'
REASON_SOLD = "sold"                      # an issued/checked_in ticket holds it
REASON_LOCKED = "locked"                  # a live order holds an unreleased lock
REASON_LOCK_CONTENTION = "lock_contention"  # lost the INSERT race; holder not yet visible


@dataclass(frozen=True)
class SeatConflict:
    """Exactly which seat, and exactly why, in a form a UI can render."""

    seat_uid: str
    reason: str
    seat_id: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SeatsUnavailable(EngineConflict):
    """All-or-nothing failure of a lock attempt.

    Carries one SeatConflict per *offending* seat - never the whole requested
    set - so the caller can repaint precisely those.
    """

    code = "seats_unavailable"

    def __init__(self, conflicts: list[SeatConflict], message: str | None = None) -> None:
        self.conflicts = list(conflicts)
        uids = ", ".join(c.seat_uid for c in self.conflicts) or "(none)"
        super().__init__(message or f"seats unavailable: {uids}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "conflicts": [c.as_dict() for c in self.conflicts],
        }

    def uids(self) -> list[str]:
        return [c.seat_uid for c in self.conflicts]

    def reasons(self) -> dict[str, str]:
        return {c.seat_uid: c.reason for c in self.conflicts}


# ── Order lifecycle ─────────────────────────────────────────────────────────
class OrderNotLive(EngineConflict):
    """The order is expired, cancelled or already completed.

    `detail` carries `status` and `expired` so the caller can tell "you took too
    long" apart from "you already paid".
    """

    code = "order_not_live"


class ExtensionAlreadyUsed(EngineConflict):
    """The single permitted +4:00 has already been granted (Decision 3)."""

    code = "extension_already_used"


class InvalidTicketTransition(EngineConflict):
    code = "invalid_ticket_transition"


class PricingUnavailable(EngineServiceError):
    """A seat has neither a price override nor a priced category."""

    code = "pricing_unavailable"
    http_status = 422


class LayoutInvalid(ValidationError):
    """layout_data cannot be materialised into inventory."""

    code = "layout_invalid"


class ConcurrentPublish(EngineConflict):
    """Two publishes of the same performance overlapped.

    Not reachable in normal flow - publishing is an operator action - but the
    UNIQUE(performance_id, seat_uid) backstop can still fire, and when it does it
    must surface as this rather than as a raw IntegrityError.
    """

    code = "concurrent_publish"
