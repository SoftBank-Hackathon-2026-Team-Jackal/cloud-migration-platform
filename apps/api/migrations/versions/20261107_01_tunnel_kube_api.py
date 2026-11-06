"""Tunnel status K3s API check (ticket 73).

Revision ID: 20261107_01
Revises: 20261106_03
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "20261107_01"
down_revision = "20261106_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_tunnel_status") as batch:
        batch.add_column(sa.Column("kube_api_ok", sa.Boolean()))


def downgrade() -> None:
    with op.batch_alter_table("agent_tunnel_status") as batch:
        batch.drop_column("kube_api_ok")
