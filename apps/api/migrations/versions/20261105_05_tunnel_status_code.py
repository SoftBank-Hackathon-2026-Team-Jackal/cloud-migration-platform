"""Tunnel status failure code (ticket 17 W8).

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
    with op.batch_alter_table("agent_tunnel_status") as batch:
        batch.add_column(sa.Column("code", sa.String(length=64)))


def downgrade() -> None:
    with op.batch_alter_table("agent_tunnel_status") as batch:
        batch.drop_column("code")
