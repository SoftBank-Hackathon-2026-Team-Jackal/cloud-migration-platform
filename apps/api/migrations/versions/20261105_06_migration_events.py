"""Migration event store and operational log (ticket 24).

Revision ID: 20261105_06
Revises: 20261105_04

Ticket 23 adds 20261105_05 (environment connections) on another branch; whichever merges second
re-chains its down_revision onto the other.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_06"
down_revision = "20261105_04"
branch_labels = None
depends_on = None

_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "migration_events",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column("migration_id", sa.String(length=120), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("migration_id", "seq", name="uq_migration_events_seq"),
        sa.UniqueConstraint(
            "migration_id", "idempotency_key", name="uq_migration_events_idempotency"
        ),
    )
    op.create_table(
        "migration_logs",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column("migration_id", sa.String(length=120), nullable=False),
        sa.Column("at", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("activity", sa.String(length=120), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
    )
    op.create_index("ix_migration_logs_migration_id", "migration_logs", ["migration_id"])


def downgrade() -> None:
    op.drop_index("ix_migration_logs_migration_id", table_name="migration_logs")
    op.drop_table("migration_logs")
    op.drop_table("migration_events")
