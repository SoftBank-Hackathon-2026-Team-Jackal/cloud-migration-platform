"""Migration scope analyses and confirmed scopes (ticket 56, #219).

Only names, ARNs and evidence locations read from the source cluster; no Secret or env values.

Revision ID: 20261106_03
Revises: 20261106_02
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from alembic import op

revision = "20261106_03"
down_revision = "20261106_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scope_analyses",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("connection_id", sa.String(length=40), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )
    op.create_index("ix_scope_analyses_connection_id", "scope_analyses", ["connection_id"])
    op.create_table(
        "scope_confirmations",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("analysis_id", sa.String(length=40), nullable=False),
        sa.Column("included", sa.JSON(), nullable=False),
        sa.Column("excluded", sa.JSON(), nullable=False),
        sa.Column("confirmed_at", sa.Integer(), nullable=False),
    )
    op.create_index("ix_scope_confirmations_analysis_id", "scope_confirmations", ["analysis_id"])


def downgrade() -> None:
    op.drop_index("ix_scope_confirmations_analysis_id", table_name="scope_confirmations")
    op.drop_table("scope_confirmations")
    op.drop_index("ix_scope_analyses_connection_id", table_name="scope_analyses")
    op.drop_table("scope_analyses")
