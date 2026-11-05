"""Environment connections: AWS role delegation and GCP target records (ticket 23, ADR-005).

Only non-secret data: Role ARN, platform-issued External ID and verification metadata.

Revision ID: 20261105_05
Revises: 20261105_04
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_05"
down_revision = "20261105_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environment_connections",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("region", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True, unique=True),
        sa.Column("role_arn", sa.String(length=600), nullable=True),
        sa.Column("account_id", sa.String(length=12), nullable=True),
        sa.Column("project_id", sa.String(length=30), nullable=True),
        sa.Column("expected_principal", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("principal", sa.String(length=600), nullable=True),
        sa.Column("last_code", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.Integer(), nullable=True),
        sa.Column("last_verified_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("environment_connections")
