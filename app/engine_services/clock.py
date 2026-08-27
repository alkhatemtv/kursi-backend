"""The one place the Engine asks what time it is.

WHY A CLOCK OBJECT AND NOT `datetime.now()`
-------------------------------------------
Decision 3 makes expiry *timestamp truth*: a hold is dead the microsecond
`orders.expires_at` passes, with no sweeper in the correctness path. Two things
follow.

1. The clock must be shared by every process. If web dyno A stamped
   `expires_at` from its own wall clock and dyno B judged it against a clock
   30 s behind, a seat would be sellable twice. So the DEFAULT clock is the
   *database's* clock (`DatabaseClock`) - one clock, by construction.
2. Tests must be able to move it. Waiting eight real minutes to prove a hold
   expires is not a test. So the clock is an injectable object rather than a
   direct call to `datetime.now()`, and `ManualClock` lets a test say "it is now
   nine minutes later" without sleeping and without stubbing the database.

Every service call resolves `now` ONCE at the top and threads that single value
through its predicates and its writes. That is what makes a call internally
consistent: the reclaim, the INSERT and the verification all judge the world
against the same instant.

THREADING
---------
The active clock is a module global behind a lock, deliberately *not* a
`ContextVar`: the race tests run service calls in real `threading.Thread`s, and
a ContextVar set in the parent is not visible in a thread it spawns.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

UTC = timezone.utc

#: SQLite stores DATETIME as a naive string, so values read back carry no tzinfo.
#: This is the single place that discrepancy is normalised.
_SQLITE_NOW = "%Y-%m-%d %H:%M:%f"


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime; convert an aware one. Idempotent.

    Needed because SQLite hands back naive datetimes for `timestamptz` columns
    while PostgreSQL hands back aware ones. Comparing the two in Python raises
    TypeError, so anything that leaves the persistence layer goes through here.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Clock:
    """Answers "what time is it", given a session to ask through."""

    def now(self, session: Session) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError


class DatabaseClock(Clock):
    """The production clock: the database server's own wall clock.

    PostgreSQL: `clock_timestamp()`, deliberately not `now()`. `now()` is the
    *transaction* start time, which would judge a long-running transaction
    against a stale instant.

    SQLite: `strftime('%Y-%m-%d %H:%M:%f', 'now')`, which is UTC with
    millisecond resolution. Plain `CURRENT_TIMESTAMP` is whole seconds - too
    coarse to reason about an eight-minute hold near its boundary.
    """

    def now(self, session: Session) -> datetime:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            raw = session.execute(
                select(func.strftime(_SQLITE_NOW, "now"))
            ).scalar_one()
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
        value = session.execute(select(func.clock_timestamp())).scalar_one()
        return as_utc(value)


class ManualClock(Clock):
    """A clock a test drives by hand. Never used in production code paths.

    Thread-safe because the race tests read it from worker threads while the
    main thread may be advancing it.
    """

    def __init__(self, instant: datetime) -> None:
        self._instant = as_utc(instant)
        self._lock = threading.Lock()

    def now(self, session: Session | None = None) -> datetime:
        with self._lock:
            return self._instant

    def set(self, instant: datetime) -> datetime:
        with self._lock:
            self._instant = as_utc(instant)
            return self._instant

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            self._instant = self._instant + delta
            return self._instant

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ManualClock {self._instant.isoformat()}>"


_CLOCK_LOCK = threading.Lock()
_CLOCK: Clock = DatabaseClock()


def get_clock() -> Clock:
    with _CLOCK_LOCK:
        return _CLOCK


def set_clock(clock: Clock) -> Clock:
    """Install `clock`, returning the one it replaced."""
    global _CLOCK
    with _CLOCK_LOCK:
        previous, _CLOCK = _CLOCK, clock
    return previous


@contextmanager
def using_clock(clock: Clock):
    previous = set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)


def now(session: Session) -> datetime:
    """The instant a service call should judge the whole world against."""
    return get_clock().now(session)
