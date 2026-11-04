"""Agent last-seen, operator revocation and registration-time cluster binding (ticket 15, #69).

Revision ID: 20261105_02
Revises: 20261105_01
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_02"
down_revision = "20261105_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("registered_agents") as batch:
        batch.add_column(sa.Column("last_seen_at", sa.Integer()))
        batch.add_column(sa.Column("revoked_at", sa.Integer()))
    with op.batch_alter_table("agent_enrollments") as batch:
        batch.alter_column("cluster_uid", existing_type=sa.String(length=120), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("agent_enrollments") as batch:
        batch.alter_column("cluster_uid", existing_type=sa.String(length=120), nullable=False)
    with op.batch_alter_table("registered_agents") as batch:
        batch.drop_column("revoked_at")
        batch.drop_column("last_seen_at")
