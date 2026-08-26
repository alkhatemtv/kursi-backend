"""baseline: current production schema

This is the BASELINE revision. It reproduces the schema that
`Base.metadata.create_all()` had already created on the live Railway database,
exactly as the models define it today. It was produced with
`alembic revision --autogenerate` against an empty throwaway SQLite database and
then hand-verified column-by-column against `app/models.py`.

IMPORTANT - existing databases (production/staging):
    The production database ALREADY has these tables. Do NOT run `upgrade` there.
    Run `alembic stamp d29b3ede11f0` once instead, which records "this database is
    already at the baseline" without touching a single table. `scripts/migrate.py`
    does this automatically. See MIGRATIONS.md.

Revision ID: d29b3ede11f0
Revises:
Create Date: 2026-08-26 19:27:02.310532
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d29b3ede11f0"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the full baseline schema on an empty database."""
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auth0_sub", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("org_name", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_auth0_sub"), "users", ["auth0_sub"], unique=True)
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organizer_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("venue", sa.String(), nullable=True),
        sa.Column("event_date", sa.String(), nullable=True),
        sa.Column("icon", sa.String(), nullable=False),
        sa.Column("tag", sa.String(), nullable=False),
        sa.Column("stage_w", sa.Integer(), nullable=False),
        sa.Column("stage_h", sa.Integer(), nullable=False),
        sa.Column("seats", sa.JSON(), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("performer", sa.String(), nullable=True),
        sa.Column("gallery", sa.JSON(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("min_price", sa.Float(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organizer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_events_event_date"), "events", ["event_date"], unique=False)
    op.create_index(op.f("ix_events_id"), "events", ["id"], unique=False)
    op.create_index(op.f("ix_events_min_price"), "events", ["min_price"], unique=False)
    op.create_index(op.f("ix_events_name"), "events", ["name"], unique=False)
    op.create_index(op.f("ix_events_organizer_id"), "events", ["organizer_id"], unique=False)
    op.create_index(op.f("ix_events_tag"), "events", ["tag"], unique=False)
    op.create_index(op.f("ix_events_venue"), "events", ["venue"], unique=False)
    op.create_index(op.f("ix_events_view_count"), "events", ["view_count"], unique=False)

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("seats", sa.JSON(), nullable=False),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column("customer_name", sa.String(), nullable=True),
        sa.Column("customer_email", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payment_ref", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bookings_customer_id"), "bookings", ["customer_id"], unique=False)
    op.create_index(op.f("ix_bookings_event_id"), "bookings", ["event_id"], unique=False)
    op.create_index(op.f("ix_bookings_id"), "bookings", ["id"], unique=False)
    op.create_index(op.f("ix_bookings_ref"), "bookings", ["ref"], unique=True)

    op.create_table(
        "wishlist",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        # Order matches what Base.metadata.create_all() emitted on the live DB.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("user_id", "event_id"),
    )
    op.create_index(op.f("ix_wishlist_event_id"), "wishlist", ["event_id"], unique=False)
    op.create_index(op.f("ix_wishlist_user_id"), "wishlist", ["user_id"], unique=False)

    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_refunds_booking_id"), "refunds", ["booking_id"], unique=False)
    op.create_index(op.f("ix_refunds_id"), "refunds", ["id"], unique=False)


def downgrade() -> None:
    """Drop the entire schema. Never run this against a database with real data."""
    op.drop_index(op.f("ix_refunds_id"), table_name="refunds")
    op.drop_index(op.f("ix_refunds_booking_id"), table_name="refunds")
    op.drop_table("refunds")

    op.drop_index(op.f("ix_wishlist_user_id"), table_name="wishlist")
    op.drop_index(op.f("ix_wishlist_event_id"), table_name="wishlist")
    op.drop_table("wishlist")

    op.drop_index(op.f("ix_bookings_ref"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_id"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_event_id"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_customer_id"), table_name="bookings")
    op.drop_table("bookings")

    op.drop_index(op.f("ix_events_view_count"), table_name="events")
    op.drop_index(op.f("ix_events_venue"), table_name="events")
    op.drop_index(op.f("ix_events_tag"), table_name="events")
    op.drop_index(op.f("ix_events_organizer_id"), table_name="events")
    op.drop_index(op.f("ix_events_name"), table_name="events")
    op.drop_index(op.f("ix_events_min_price"), table_name="events")
    op.drop_index(op.f("ix_events_id"), table_name="events")
    op.drop_index(op.f("ix_events_event_date"), table_name="events")
    op.drop_table("events")

    op.drop_index(op.f("ix_users_id"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_index(op.f("ix_users_auth0_sub"), table_name="users")
    op.drop_table("users")
