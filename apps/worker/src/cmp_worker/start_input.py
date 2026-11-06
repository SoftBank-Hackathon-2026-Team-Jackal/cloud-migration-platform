"""The input that starts the real first cycle (ticket 22). Pure, so it can be checked without a server."""

import json
from typing import Any

from cmp_contracts.analysis import AnalysisInput
from cmp_contracts.provisioning import ContentRef
from cmp_data.handoff import DataMigrationInput
from cmp_scanners.source import SourceFetchError, check_source_input
from cmp_traffic import TrafficShiftInput

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
    data_migration_json: str | None = None,
    traffic_json: str | None = None,
) -> dict[str, Any]:
    """`{"project_id", "analysis": AnalysisInput, "sandbox": SandboxSettings}` as MigrationWorkflow expects.

    The analysis input never carries a credential, only the name of a local profile. The migration
    ID is the Workflow ID, which the workflow fills in itself.
    """
    try:
        check_source_input(repository, commit)
    except SourceFetchError as exc:
        raise ValueError(str(exc)) from None
    planning = PlanningSettings.model_validate(json.loads(planning_json)) if planning_json else None
    analysis = AnalysisInput(
        migration_id=project_id,
        repository=repository,
        requested_commit=commit,
        manifests_dir=manifests_dir,
        cluster_name=cluster_name,
        credential_ref=credential_ref,
        source_access_ref=dev_ref() if source_access == "dev-env" else None,
        # Ticket 31: the same value the web start gives (the planning target's provider).
        target_provider=planning.target_selection.provider if planning else None,
    )
    sandbox = SandboxSettings.model_validate(json.loads(sandbox_json) if sandbox_json else {})
    env_ref = (
        ContentRef.model_validate(json.loads(env_file_ref_json)) if env_file_ref_json else None
    )
    # specs/data-migration: 원본 DB·앱과 대상 터널 주소. 없으면 data_copy는 scaffold로 남는다.
    data_migration = (
        DataMigrationInput.model_validate_json(data_migration_json).model_dump(mode="json")
        if data_migration_json
        else None
    )
    # #128: 중계 서버 큐와 전환 기준. 없으면 traffic 단계는 scaffold로 남는다.
    traffic = (
        TrafficShiftInput.model_validate_json(traffic_json).model_dump(mode="json")
        if traffic_json
        else None
    )
    return {
        "project_id": project_id,
        "analysis": analysis.model_dump(mode="json"),
        "sandbox": sandbox.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        or None,
        **({"planning": planning.model_dump(mode="json")} if planning else {}),
        **({"env_file_ref": env_ref.model_dump(mode="json")} if env_ref else {}),
        **({"data_migration": data_migration} if data_migration else {}),
        **({"traffic": traffic} if traffic else {}),
    }
