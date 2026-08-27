"""Transaction boundaries for the Engine services.

THE PATTERN, AND WHY THIS ONE
-----------------------------
Every public service function takes a `Session` it does not own and wraps its
work in exactly one `unit_of_work(session)` block: commit on success, rollback
on any exception. One public call == one database transaction.

The alternatives were rejected for concrete reasons:

* *Services open their own sessions.* Then a caller could not compose two calls,
  and FastAPI's request-scoped `get_db` session would be bypassed.
* *Services never commit; the caller does.* This is the usual advice, and it is
  wrong here. The locking engine's arbiter is a UNIQUE index: a lock only
  actually excludes a competing lock once it is COMMITTED. A service that leaves
  its holds uncommitted would hand the caller an order that holds nothing, and
  the race test could not be written at all. The hold must be durable when
  `create_draft_order` returns.
* *Nested transactions / SAVEPOINT for the conflict path.* `pysqlite` does not
  emit SAVEPOINT reliably without connection-event surgery, so the conflict path
  deliberately rolls back the WHOLE transaction and re-diagnoses in a fresh one.
  That is also more honest: the diagnosis then reports what is committed and
  true, not what was visible inside a doomed transaction.

`unit_of_work` refuses to nest. A service that wanted to call another service
inside its own transaction would silently commit half the outer work at the
inner block's exit; making that an error keeps the "one call, one transaction"
rule true rather than aspirational.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.orm import Session


class NestedUnitOfWork(RuntimeError):
    """Raised when a unit of work is opened inside another one."""


_ACTIVE = "_engine_uow_active"


@contextmanager
def unit_of_work(session: Session):
    """Commit on success, roll back on failure. One per public service call."""
    if getattr(session, _ACTIVE, False):
        raise NestedUnitOfWork(
            "unit_of_work is already active on this session. Engine services own "
            "their own transaction; call the inner helper, not the public function."
        )
    setattr(session, _ACTIVE, True)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        setattr(session, _ACTIVE, False)
