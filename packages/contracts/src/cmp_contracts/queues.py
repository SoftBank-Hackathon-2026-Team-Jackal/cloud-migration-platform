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


# Ticket 48 (#183): the API asks the provisioning worker (which holds the GCP identity) to check a
# GCP_TARGET connection. Input: expected principal email; result: {"code", "principal"}.
GCP_CONNECTION_CHECK_WORKFLOW_NAME = "GcpConnectionCheckWorkflow"
GCP_CONNECTION_CHECK_ACTIVITY = "verify_gcp_connection"
