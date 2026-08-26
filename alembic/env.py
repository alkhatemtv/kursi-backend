"""Alembic environment for Kursi.

Wiring notes:
  * The URL is NEVER read from alembic.ini. It comes from `app.config.settings`,
    which reads the `DATABASE_URL` environment variable (same source the app uses),
    so `alembic` and the running app can never disagree about which database
    they are pointed at.
  * `target_metadata` is the live `Base.metadata` from `app.database`, populated by
    importing `app.models`. Autogenerate therefore diffs against the real models.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.database import Base

# Importing the models module registers every table on Base.metadata.
# Without this import autogenerate would see an empty schema and try to drop
# every table in the database.
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Resolve the database URL from application settings (env-driven).

    An explicit `-x db_url=...` on the alembic command line wins, which is how the
    test/verification tooling points Alembic at a throwaway database.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    return x_args.get("db_url") or settings.database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it against a database ('--sql' mode)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead. Harmless no-op on PostgreSQL.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
