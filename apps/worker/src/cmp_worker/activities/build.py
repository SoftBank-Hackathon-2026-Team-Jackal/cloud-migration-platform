from temporalio import activity


@activity.defn
async def prepare_image(project_id: str) -> dict[str, str]:
    """Example boundary for AI, sandbox, image build and registry; no image is built."""
    activity.logger.info("Scaffold build for %s", project_id)
    return {"step": "build", "mode": "scaffold", "detail": "No image built or pushed"}
