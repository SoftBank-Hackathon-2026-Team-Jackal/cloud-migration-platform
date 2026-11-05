"""Planning input registration and Plan publication input Digest (ticket 18).

Revision ID: 20261105_04
Revises: 20261105_03
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_04"
down_revision = "20261105_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provisioning_planning_inputs",
        sa.Column("migration_id", sa.String(length=120), primary_key=True),
        sa.Column("registration", sa.JSON(), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("registered_at", sa.Integer(), nullable=False),
    )
    op.create_table(
        "provisioning_plan_publications",
        sa.Column("plan_id", sa.String(length=120), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "revision"],
            ["provisioning_plan_revisions.plan_id", "provisioning_plan_revisions.revision"],
        ),
    )


def downgrade() -> None:
    op.drop_table("provisioning_plan_publications")
    op.drop_table("provisioning_planning_inputs")
