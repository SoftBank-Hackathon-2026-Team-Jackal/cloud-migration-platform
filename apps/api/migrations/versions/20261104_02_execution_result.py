"""Execution result code and resource references on the execution marker (ticket 08).

Revision ID: 20261104_02
Revises: 20261104_01
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "20261104_02"
down_revision = "20261104_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provisioning_executions") as batch:
        batch.add_column(sa.Column("code", sa.String(length=64)))
        batch.add_column(sa.Column("resource_refs", sa.JSON()))


def downgrade() -> None:
    with op.batch_alter_table("provisioning_executions") as batch:
        batch.drop_column("resource_refs")
        batch.drop_column("code")
