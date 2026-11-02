from temporalio import activity


@activity.defn
async def analyze_project(project_id: str) -> dict[str, str]:
    """Example boundary for source and application analysis; performs no scan."""
    activity.logger.info("Scaffold analysis for %s", project_id)
    return {"step": "analysis", "mode": "scaffold", "detail": "No cloud scan performed"}
