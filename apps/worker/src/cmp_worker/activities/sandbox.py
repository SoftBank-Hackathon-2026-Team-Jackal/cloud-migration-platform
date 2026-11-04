"""Inert activity used to verify Sandbox worker registration."""

from temporalio import activity


@activity.defn
async def sandbox_queue_probe() -> str:
    """Report only that the scaffold worker processed a task."""
    return "scaffold"


@activity.defn
async def request_build(payload: dict) -> dict:
    return {"build_ref": "scaffold-build"}


@activity.defn
async def prepare_sandbox(payload: dict) -> dict:
    return {"sandbox_ref": "scaffold-sandbox"}


@activity.defn
async def render_sandbox(payload: dict) -> dict:
    return {"render_ref": "scaffold-render"}


@activity.defn
async def apply_sandbox(payload: dict) -> dict:
    return {"apply_ref": "scaffold-apply"}


@activity.defn
async def run_sandbox_tests(payload: dict) -> dict:
    return {"test_ref": "scaffold-test"}


@activity.defn
async def judge_sandbox(payload: dict) -> dict:
    return {"passed": True, "failure_class": None}


@activity.defn
async def classify_sandbox_failure(payload: dict) -> str:
    return payload["failure_class"]


@activity.defn
async def fix_sandbox(payload: dict) -> dict:
    return {"fix_ref": "scaffold-fix"}


@activity.defn
async def publish_final_report(payload: dict) -> dict:
    return {"report_ref": "scaffold-report"}


@activity.defn
async def teardown_sandbox(payload: dict) -> dict:
    return {"cleanup_status": "CLEANED"}
