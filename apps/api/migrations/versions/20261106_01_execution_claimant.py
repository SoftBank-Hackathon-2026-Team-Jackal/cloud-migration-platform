"""Execution marker claimant for idempotent claim retries (ticket 49 F10).

Revision ID: 20261106_01
Revises: 20261105_07
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from alembic import op

revision = "20261106_01"
down_revision = "20261105_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provisioning_executions") as batch:
        batch.add_column(sa.Column("claimed_by", sa.String(length=255)))


def downgrade() -> None:
    with op.batch_alter_table("provisioning_executions") as batch:
        batch.drop_column("claimed_by")
