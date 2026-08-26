"""Alembic revision introspection.

Two consumers:
  * the startup safety check in `app.main` - logs loudly if the database schema is
    behind the code, but never crashes the app;
  * `GET /health`, which surfaces the same information for humans and uptime checks.

Everything in here is defensive: a broken/missing Alembic setup, an unreachable
database, or a database with no `alembic_version` table must degrade to "unknown"
rather than take the API down.
"""
from __future__ import annotations

import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import Engine

logger = logging.getLogger("kursi")

# Repo root - the directory holding alembic.ini
ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"


@lru_cache(maxsize=1)
def get_head_revision() -> str | None:
    """The newest revision id on disk (the migration scripts), or None if unavailable.

    Cached: the migration scripts cannot change while the process is running.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
        return script.get_current_head()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not read Alembic head revision: %s", e)
        return None


def get_db_revision(engine: Engine) -> str | None:
    """The revision id stamped in the database, or None if the DB is unreachable
    or has never been stamped (no `alembic_version` table)."""
    try:
        from alembic.migration import MigrationContext

        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not read database Alembic revision: %s", e)
        return None


def check_migration_state(engine: Engine) -> dict[str, object]:
    """Compare the database revision against the code's head revision.

    Returns a dict suitable for embedding in the /health payload. Never raises.
    """
    head = get_head_revision()
    current = get_db_revision(engine)
    if head is None or current is None:
        state = "unknown"
    elif current == head:
        state = "up_to_date"
    else:
        state = "out_of_date"
    return {"db_revision": current, "head_revision": head, "state": state}


def log_migration_state(engine: Engine) -> dict[str, object]:
    """Startup safety check. Logs a warning on drift; NEVER crashes the app.

    Deliberately non-fatal: a schema that is one revision behind is usually still
    serving traffic fine, and taking the API down on boot would turn a warning
    into an outage.
    """
    info = check_migration_state(engine)
    state, current, head = info["state"], info["db_revision"], info["head_revision"]

    if state == "up_to_date":
        logger.info("Database schema is at head revision %s", head)
    elif state == "out_of_date":
        logger.warning(
            "DATABASE SCHEMA OUT OF DATE: database is at revision %r but the code "
            "expects %r. Run 'alembic upgrade head' (see MIGRATIONS.md). "
            "The app will keep serving, but requests touching new columns may fail.",
            current,
            head,
        )
    elif current is None and head is not None:
        logger.warning(
            "Database has no alembic_version table (never stamped). Expected head "
            "revision %r. If this database already has the schema, run "
            "'alembic stamp head' once - see MIGRATIONS.md.",
            head,
        )
    else:
        logger.warning("Could not determine migration state: %s", info)
    return info


@lru_cache(maxsize=1)
def get_app_version() -> str:
    """A cheap 'git-ish' build identifier for /health.

    Preference order:
      1. an explicit env var set by the platform (Railway injects
         RAILWAY_GIT_COMMIT_SHA on every deploy - no git binary needed);
      2. `git rev-parse --short HEAD` when running from a checkout;
      3. "unknown".
    """
    for var in ("GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA", "SOURCE_VERSION"):
        sha = os.environ.get(var)
        if sha:
            return sha[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # pragma: no cover - git absent in the deployed image
        pass
    return "unknown"
