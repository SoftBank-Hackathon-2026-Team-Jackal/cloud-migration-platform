from temporalio import activity


@activity.defn
async def copy_data(project_id: str) -> dict[str, str]:
    """Example replication boundary; no data is read or copied."""
    activity.logger.info("Scaffold data migration for %s", project_id)
    return {"step": "data_copy", "mode": "scaffold", "detail": "No data copied"}


@activity.defn
async def shift_traffic(project_id: str) -> dict[str, str]:
    """Example routing boundary; no DNS or HTTP routing is changed."""
    activity.logger.info("Scaffold traffic shift for %s", project_id)
    return {"step": "traffic", "mode": "scaffold", "detail": "No traffic changed"}


@activity.defn
async def verify_target(project_id: str) -> dict[str, str]:
    """Example final verification boundary; no remote target is checked."""
    activity.logger.info("Scaffold verification for %s", project_id)
    return {"step": "verification", "mode": "scaffold", "detail": "No target verified"}
