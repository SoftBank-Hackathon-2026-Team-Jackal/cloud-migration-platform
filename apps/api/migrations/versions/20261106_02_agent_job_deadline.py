"""Agent job deadline and operator cancel request (ticket 52, #187).

Revision ID: 20261106_02
Revises: 20261106_01
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from alembic import op

revision = "20261106_02"
down_revision = "20261106_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_jobs") as batch:
        batch.add_column(sa.Column("deadline_at", sa.Integer()))
        batch.add_column(sa.Column("cancel_requested_at", sa.Integer()))


def downgrade() -> None:
    with op.batch_alter_table("agent_jobs") as batch:
        batch.drop_column("cancel_requested_at")
        batch.drop_column("deadline_at")
