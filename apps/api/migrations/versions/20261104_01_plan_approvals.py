"""Plan Revision, approver, approval decision and execution marker records (ticket 07).

Revision ID: 20261104_01
Revises: 20261103_01
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "20261104_01"
down_revision = "20261103_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provisioning_plan_revisions",
        sa.Column("plan_id", sa.String(length=120), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("migration_id", sa.String(length=120), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.Integer(), nullable=False),
    )
    op.create_table(
        "provisioning_approvers",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("credential_digest", sa.String(length=64), nullable=False, unique=True),
        sa.Column("migration_ids", sa.JSON(), nullable=False),
    )
    op.create_table(
        "provisioning_approvals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("plan_id", sa.String(length=120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column(
            "approver_id",
            sa.String(length=120),
            sa.ForeignKey("provisioning_approvers.id"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("preview_digest", sa.String(length=64), nullable=False),
        sa.Column("decided_at", sa.Integer(), nullable=False),
        sa.Column("delivery_status", sa.String(length=12), nullable=False),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("delivery_error", sa.String(length=64)),
        sa.UniqueConstraint("plan_id", "revision", name="uq_provisioning_approvals_revision"),
        sa.ForeignKeyConstraint(
            ["plan_id", "revision"],
            ["provisioning_plan_revisions.plan_id", "provisioning_plan_revisions.revision"],
        ),
    )
    op.create_table(
        "provisioning_executions",
        sa.Column("plan_id", sa.String(length=120), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("step", sa.String(length=40), primary_key=True),
        sa.Column(
            "approval_id",
            sa.String(length=36),
            sa.ForeignKey("provisioning_approvals.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer()),
    )


def downgrade() -> None:
    op.drop_table("provisioning_executions")
    op.drop_table("provisioning_approvals")
    op.drop_table("provisioning_approvers")
    op.drop_table("provisioning_plan_revisions")
