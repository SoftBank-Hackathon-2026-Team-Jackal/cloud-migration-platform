"""Provider execution dispatch shared by the approval Workflow and Workers (ticket 08, E1).

Pure and deterministic: safe to call inside Workflow code.
"""

STEPS = {
    ("AWS", "INFRASTRUCTURE"): ("execute_aws_plan",),
    ("AWS", "WORKLOADS"): ("execute_aws_workloads",),
    ("AWS", "FULL"): ("execute_aws_plan", "execute_aws_workloads"),
    ("GCP", "INFRASTRUCTURE"): ("execute_gcp_plan",),
    ("GCP", "WORKLOADS"): ("execute_gcp_workloads",),
    ("GCP", "FULL"): ("execute_gcp_plan", "execute_gcp_workloads"),
    ("ON_PREM", "WORKLOADS"): ("execute_on_prem_plan",),
    ("ON_PREM", "FULL"): ("execute_on_prem_plan",),
}


def execution_steps(provider: str, scope: str) -> tuple[str, ...]:
    """Ordered provider Activity names; empty when the target has no execution path yet."""
    return STEPS.get((provider, scope), ())
