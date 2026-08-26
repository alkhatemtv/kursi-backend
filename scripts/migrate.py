"""Apply database migrations - the single entry point used by the deploy.

Run as:  python -m scripts.migrate   (or  python scripts/migrate.py)

It does one of three things, decided by inspecting the database:

  1. Database has no tables at all (a brand-new staging DB)
     -> `alembic upgrade head`, which creates everything from the baseline.

  2. Database already has the application tables but no `alembic_version` row
     (this is TODAY'S PRODUCTION DB - it was built by `Base.metadata.create_all`
     before Alembic existed)
     -> `alembic stamp <baseline>` then `alembic upgrade head`.
        Stamping only writes one row to `alembic_version`; it does NOT touch,
        rewrite, or drop any existing table or data. Without it the deploy would
        try to CREATE TABLE users on a database that already has one and fail.

  3. Database is already under Alembic control
     -> `alembic upgrade head` (a no-op when already at head).

The database URL is never hardcoded - it comes from DATABASE_URL via
app.config.settings, exactly like the app itself.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `python scripts/migrate.py` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402

from app.config import settings  # noqa: E402

# The baseline revision. A pre-Alembic database is stamped with exactly this.
BASELINE_REVISION = "d29b3ede11f0"

# Tables that must all be present for a database to count as "already has the schema".
EXPECTED_TABLES = {"users", "events", "bookings", "refunds", "wishlist"}

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate")


def _safe_url(url: str) -> str:
    """Redact credentials so we can log which database we are pointed at."""
    if "@" in url and "//" in url:
        scheme, rest = url.split("//", 1)
        return f"{scheme}//***@{rest.split('@', 1)[1]}"
    return url


def main() -> int:
    url = settings.database_url
    if not url:
        log.error("DATABASE_URL is not set - refusing to run migrations.")
        return 1

    log.info("Target database: %s", _safe_url(url))

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            stamped = MigrationContext.configure(conn).get_current_revision()
            tables = set(inspect(conn).get_table_names())
    finally:
        engine.dispose()

    has_schema = EXPECTED_TABLES.issubset(tables)

    if stamped is None and has_schema:
        # Case 2 - the pre-Alembic production database.
        log.warning(
            "Database already has the application tables but is not under Alembic "
            "control. Stamping it at the baseline revision %s (writes one row to "
            "alembic_version; no table is modified).",
            BASELINE_REVISION,
        )
        command.stamp(cfg, BASELINE_REVISION)
    elif stamped is None:
        # Case 1 - empty database.
        log.info("Empty database detected - creating the schema from scratch.")
    else:
        # Case 3 - normal path.
        log.info("Database is at revision %s.", stamped)

    log.info("Running: alembic upgrade head")
    command.upgrade(cfg, "head")
    log.info("Migrations complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
