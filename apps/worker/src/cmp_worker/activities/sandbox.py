"""Inert activity used to verify Sandbox worker registration."""

from temporalio import activity


@activity.defn
async def sandbox_queue_probe() -> str:
    """Report only that the scaffold worker processed a task."""
    return "scaffold"
