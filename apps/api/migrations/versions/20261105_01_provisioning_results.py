"""Immutable ProvisioningResult per Plan Revision for the Migration handoff (ticket 13).

Revision ID: 20261105_01
Revises: 20261104_02
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_01"
down_revision = "20261104_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provisioning_results",
        sa.Column("plan_id", sa.String(length=120), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("migration_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provisioning_results")
