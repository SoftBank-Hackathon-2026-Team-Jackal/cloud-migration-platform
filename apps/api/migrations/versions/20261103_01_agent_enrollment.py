"""Owner-scoped on-premises Agent enrollment and work references.

Revision ID: 20261103_01
Revises:
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "20261103_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("agent_owners", sa.Column("id", sa.String(length=120), primary_key=True))
    op.create_table(
        "agent_enrollments",
        sa.Column("token_digest", sa.String(length=64), primary_key=True),
        sa.Column(
            "owner_id", sa.String(length=120), sa.ForeignKey("agent_owners.id"), nullable=False
        ),
        sa.Column("agent_id", sa.String(length=120), nullable=False, unique=True),
        sa.Column("cluster_uid", sa.String(length=120), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.Integer()),
    )
    op.create_table(
        "registered_agents",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "owner_id", sa.String(length=120), sa.ForeignKey("agent_owners.id"), nullable=False
        ),
        sa.Column("cluster_uid", sa.String(length=120), nullable=False),
        sa.Column("credential_digest", sa.String(length=64), nullable=False),
    )
    op.create_table(
        "agent_jobs",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column(
            "owner_id", sa.String(length=120), sa.ForeignKey("agent_owners.id"), nullable=False
        ),
        sa.Column(
            "agent_id", sa.String(length=120), sa.ForeignKey("registered_agents.id"), nullable=False
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.JSON()),
    )
    op.create_index("ix_agent_jobs_agent_status", "agent_jobs", ["agent_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_jobs_agent_status", table_name="agent_jobs")
    op.drop_table("agent_jobs")
    op.drop_table("registered_agents")
    op.drop_table("agent_enrollments")
    op.drop_table("agent_owners")
