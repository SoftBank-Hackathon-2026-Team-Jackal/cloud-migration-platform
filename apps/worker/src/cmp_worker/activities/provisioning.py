from temporalio import activity


@activity.defn
async def prepare_plan(project_id: str) -> dict[str, str]:
    """Example plan boundary; no Terraform plan is generated."""
    activity.logger.info("Scaffold plan for %s", project_id)
    return {"step": "plan", "mode": "scaffold", "detail": "No Terraform plan generated"}


@activity.defn
async def provision_target(project_id: str) -> dict[str, str]:
    """Example apply boundary; no infrastructure is created."""
    activity.logger.info("Scaffold provisioning for %s", project_id)
    return {"step": "provisioning", "mode": "scaffold", "detail": "No infrastructure created"}
