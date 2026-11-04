"""Tunnel configuration exchange tables (ticket 16).

Revision ID: 20261105_03
Revises: 20261105_02
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_03"
down_revision = "20261105_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_tunnels",
        sa.Column("agent_id", sa.String(length=120), primary_key=True),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("tunnel_ip", sa.String(length=45), nullable=False, unique=True),
        sa.Column("updated_at", sa.Integer(), nullable=False),
    )
    op.create_table(
        "relay_servers",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("credential_digest", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
    )
    op.create_table(
        "tunnel_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "agent_tunnel_status",
        sa.Column("agent_id", sa.String(length=120), primary_key=True),
        sa.Column("tunnel_ip", sa.String(length=45), nullable=False),
        sa.Column("last_handshake_at", sa.Integer()),
        sa.Column("app_health_ok", sa.Boolean(), nullable=False),
        sa.Column("db_connect_ok", sa.Boolean(), nullable=False),
        sa.Column("reported_at", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_tunnel_status")
    op.drop_table("tunnel_settings")
    op.drop_table("relay_servers")
    op.drop_table("agent_tunnels")
