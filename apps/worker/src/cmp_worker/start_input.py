"""The input that starts the real first cycle (ticket 22). Pure, so it can be checked without a server."""

import json
from typing import Any

from cmp_contracts.analysis import AnalysisInput
from cmp_contracts.provisioning import ContentRef
from cmp_scanners.source import SourceFetchError, check_source_input

from cmp_worker.planning_handoff import PlanningSettings
from cmp_worker.sandbox_input import SandboxSettings
from cmp_worker.source_access import dev_ref


def build_start_input(
    *,
    project_id: str,
    repository: str,
    commit: str,
    manifests_dir: str | None = None,
    cluster_name: str | None = None,
    credential_ref: str | None = None,
    source_access: str | None = None,
    sandbox_json: str | None = None,
    planning_json: str | None = None,
    env_file_ref_json: str | None = None,
) -> dict[str, Any]:
    """`{"project_id", "analysis": AnalysisInput, "sandbox": SandboxSettings}` as MigrationWorkflow expects.

    The analysis input never carries a credential, only the name of a local profile. The migration
    ID is the Workflow ID, which the workflow fills in itself.
    """
    try:
        check_source_input(repository, commit)
    except SourceFetchError as exc:
        raise ValueError(str(exc)) from None
    analysis = AnalysisInput(
        migration_id=project_id,
        repository=repository,
        requested_commit=commit,
        manifests_dir=manifests_dir,
        cluster_name=cluster_name,
        credential_ref=credential_ref,
        source_access_ref=dev_ref() if source_access == "dev-env" else None,
    )
    sandbox = SandboxSettings.model_validate(json.loads(sandbox_json) if sandbox_json else {})
    planning = PlanningSettings.model_validate(json.loads(planning_json)) if planning_json else None
    env_ref = (
        ContentRef.model_validate(json.loads(env_file_ref_json)) if env_file_ref_json else None
    )
    return {
        "project_id": project_id,
        "analysis": analysis.model_dump(mode="json"),
        "sandbox": sandbox.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        or None,
        **({"planning": planning.model_dump(mode="json")} if planning else {}),
        **({"env_file_ref": env_ref.model_dump(mode="json")} if env_ref else {}),
    }
