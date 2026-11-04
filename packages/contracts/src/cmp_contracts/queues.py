"""Temporal Task Queue names shared by API and workers."""

WORKFLOW_QUEUE = "migration"
WORKFLOW_NAME = "MigrationWorkflow"
ANALYSIS_QUEUE = "analysis"
SANDBOX_QUEUE = "sandbox"
BUILD_QUEUE = "build"
PROVISIONING_QUEUE = "provisioning"
MIGRATION_QUEUE = "migration-activities"
APPROVAL_WORKFLOW_NAME = "ProvisioningApprovalWorkflow"
APPROVAL_SIGNAL = "approval_recorded"


def approval_workflow_id(plan_id: str, revision: int) -> str:
    """One approval Workflow per immutable Plan Revision (ticket 07, D4)."""
    return f"provisioning-approval-{plan_id}-r{revision}"
