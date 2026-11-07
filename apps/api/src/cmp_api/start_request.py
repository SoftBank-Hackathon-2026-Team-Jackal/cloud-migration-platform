"""Web start request → real pipeline start input (ticket 44, S1~S12).

Pure functions plus two small lookups (connection, Agent). Builds the same dict
`scripts/start_migration.py` gives `MigrationWorkflow`: `{project_id, analysis, sandbox,
planning, env_file_ref}`. Target settings come from the operator's profile catalog (S6); the
request may only choose the region, the target connection and the ON_PREM Agent (S7). Refusals
are code-only (`StartRefused`) and never echo input values.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from cmp_contracts.analysis import AnalysisInput, ConfirmedScope
from cmp_contracts.migration import MigrationRequest, StartSource, StartTarget
from cmp_contracts.plan_publish import TargetSettings
from cmp_contracts.provisioning import ContentRef, ProvisioningInputError
from cmp_contracts.provisioning_v2 import TargetSelectionV2
from cmp_data.cutover import CutoverSettings, SourceApp
from cmp_data.handoff import DataMigrationInput
from cmp_providers.aws_preview import AwsPlanInput, validate_aws_input
from cmp_providers.gcp_preview import GcpPlanInput, terraform_variables
from cmp_traffic import TrafficShiftInput
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from cmp_api.agent_egress import EGRESS, MAX_CIDRS, EgressStore
from cmp_api.agent_registry import RegisteredAgent
from cmp_api.connections import PROFILE_PREFIX, EnvironmentConnection, credential_ref
from cmp_api.plan_inputs import CLOUD_TEMPLATES
from cmp_api.scope_selection import ScopeAnalysisRecord, ScopeConfirmationRecord

_TARGET_SETTINGS = TypeAdapter(TargetSettings)
_CLOUD = {"AWS": (AwsPlanInput, validate_aws_input), "GCP": (GcpPlanInput, terraform_variables)}
_CONNECTION_KIND = {"AWS": "AWS_TARGET", "GCP": "GCP_TARGET"}
_ONPREM_FIELDS = ("namespace", "storage_class", "database_image")


class StartRefused(Exception):
    """`code` is stable and safe to show; `status` is the HTTP status."""

    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


def load_target_profiles(path: str | None) -> dict[str, dict]:
    """The catalog `{"profiles": {name: entry}}`; missing or unreadable is 503 (S6)."""
    if not path:
        raise StartRefused("TARGET_PROFILES_UNCONFIGURED", 503)
    try:
        profiles = json.loads(Path(path).read_text(encoding="utf-8"))["profiles"]
    except (OSError, ValueError, KeyError, TypeError):
        raise StartRefused("TARGET_PROFILES_UNCONFIGURED", 503) from None
    if not isinstance(profiles, dict):
        raise StartRefused("TARGET_PROFILES_UNCONFIGURED", 503)
    return profiles


def check_request(request: MigrationRequest) -> tuple[StartTarget, StartSource]:
    """S3/S4: a real start needs a source and a `.env` reference; providers must agree."""
    target = request.target
    assert target is not None
    if request.target_provider is not None and request.target_provider != target.provider:
        raise StartRefused("TARGET_PROVIDER_MISMATCH")
    if request.source is None:
        raise StartRefused("SOURCE_REQUIRED")
    if request.env_file_ref is None:
        raise StartRefused("ENV_FILE_REF_REQUIRED")
    if target.provider == "ON_PREM":
        if target.region is not None or target.credential_ref is not None:
            raise StartRefused("TARGET_FIELD_NOT_APPLICABLE")
        if target.agent_id is None:
            raise StartRefused("TARGET_AGENT_REQUIRED")
    elif target.agent_id is not None:
        raise StartRefused("TARGET_FIELD_NOT_APPLICABLE")
    return target, request.source


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def find_connection(session: Session, value: str, provider: str) -> EnvironmentConnection:
    """S8: `conn-<hex>` or `cmp-conn-<hex>` → a CONNECTED target connection of this provider."""
    connection_id = "conn-" + value.removeprefix(PROFILE_PREFIX).removeprefix("conn-")
    connection = session.get(EnvironmentConnection, connection_id)
    if connection is None:
        raise StartRefused("TARGET_CONNECTION_NOT_FOUND")
    if connection.kind != _CONNECTION_KIND[provider]:
        raise StartRefused("TARGET_CONNECTION_KIND_MISMATCH")
    if connection.status != "CONNECTED":
        raise StartRefused("TARGET_CONNECTION_NOT_READY")
    return connection


def load_scope(session: Session, scope_id: str) -> tuple[ConfirmedScope, EnvironmentConnection]:
    """06B: the stored scope confirmation, its analysis and the source connection it was read with.

    The web sends only `scope_id`; everything else comes from what the platform stored (ticket 56),
    so a request cannot name another connection or role. Refusals are codes, never values."""
    confirmation = session.get(ScopeConfirmationRecord, scope_id)
    analysis = session.get(ScopeAnalysisRecord, confirmation.analysis_id) if confirmation else None
    if confirmation is None or analysis is None:
        raise StartRefused("SCOPE_NOT_FOUND")
    connection = session.get(EnvironmentConnection, analysis.connection_id)
    if connection is None:
        raise StartRefused("SCOPE_CONNECTION_NOT_FOUND")
    if connection.kind != "AWS_SOURCE":
        raise StartRefused("SCOPE_CONNECTION_KIND_MISMATCH")
    if connection.status != "CONNECTED" or not connection.role_arn:
        raise StartRefused("SCOPE_CONNECTION_NOT_READY")
    known = {item["candidate_id"] for item in analysis.candidates}
    chosen = set(confirmation.included)
    if not chosen <= known:
        raise StartRefused("SCOPE_NOT_FOUND")
    try:
        # 06C: every candidate goes on, in the order the platform found them; `selected` says
        # which the user kept.
        scope = ConfirmedScope.model_validate(
            {
                "scope_id": confirmation.id,
                "selection": analysis.selection,
                "candidates": [
                    {**item, "selected": item["candidate_id"] in chosen}
                    for item in analysis.candidates
                ],
            }
        )
    except ValidationError:
        raise StartRefused("SCOPE_NOT_FOUND") from None
    return scope, connection


def scope_analysis_fields(scope: ConfirmedScope, credential_ref: str | None) -> dict:
    """06B: the `AnalysisInput` fields a confirmed scope fills (pure).

    `role_arns` holds only the SELECTED IAM role candidates that have an ARN, in order, once."""
    role_arns = list(
        dict.fromkeys(
            item.arn
            for item in scope.candidates
            if item.selected and item.resource_type == "IAM_ROLE" and item.arn
        )
    )
    return {
        "credential_ref": credential_ref,
        "cluster_name": scope.selection.cluster,
        "region": scope.selection.region,
        "role_arns": role_arns,
        "scope": scope.model_dump(mode="json"),
    }


def find_agent(session: Session, agent_id: str) -> RegisteredAgent:
    agent = session.get(RegisteredAgent, agent_id)
    if agent is None or agent.revoked_at is not None:
        raise StartRefused("TARGET_AGENT_NOT_FOUND")
    return agent


def _cloud_target(
    target: StartTarget, entry: dict, connection: EnvironmentConnection | None
) -> tuple[dict, str]:
    settings = dict(entry.get("settings") or {})
    if target.region is not None:
        settings["region"] = target.region
    if connection is not None:
        settings["credential_ref"] = {
            "id": credential_ref(connection) or connection.id,  # S8: AWS profile, GCP ID
            "version": "1",
        }
        if target.provider == "AWS" and connection.account_id:
            settings["account_id"] = connection.account_id
        if target.provider == "GCP" and connection.project_id:
            settings["project_id"] = connection.project_id
    region = settings.get("region")
    if not region:
        raise StartRefused("TARGET_REGION_REQUIRED")
    model, validate = _CLOUD[target.provider]
    plan = {"plan_id": "plan-start-check", "revision": 1}
    try:
        validate(
            model.model_validate(
                {
                    **settings,
                    "migration_id": "start-check",
                    "plan": {**plan, "template_version": CLOUD_TEMPLATES[target.provider]},
                }
            )
        )
    except (ValidationError, ProvisioningInputError, ValueError):
        raise StartRefused("TARGET_SETTINGS_INVALID") from None
    body = {"provider": target.provider, "settings": settings}
    if entry.get("workloads") is not None:
        body["workloads"] = entry["workloads"]
    return body, region


def planning_settings(
    target: StartTarget,
    entry: dict,
    *,
    connection: EnvironmentConnection | None = None,
    agent: RegisteredAgent | None = None,
) -> dict:
    """S6~S9, S12: `PlanningSettings` JSON with `env_file_ref: null` (the parent copies it in).

    55 (EnvPlan) does not extend this: the EnvPlan joins at `assemble_registration`.
    """
    if entry.get("provider") != target.provider:
        raise StartRefused("TARGET_PROFILE_PROVIDER_MISMATCH")
    if target.provider == "ON_PREM":
        assert agent is not None
        body = {
            "provider": "ON_PREM",
            "environment_id": entry.get("environment_id") or agent.id,
            "agent_id": agent.id,
            "cluster_uid": agent.cluster_uid,
            **{name: entry.get(name) for name in _ONPREM_FIELDS},
        }
        location = None
        access_ref = {"id": agent.id, "version": agent.cluster_uid}
    else:
        body, location = _cloud_target(target, entry, connection)
        access_ref = body["settings"].get("credential_ref")
    try:
        settings = _TARGET_SETTINGS.validate_python(body).model_dump(mode="json")
        selection = TargetSelectionV2.model_validate(
            {
                "ref": {"id": f"target-{target.profile}", "version": _digest(settings)},
                "provider": target.provider,
                "location": location,
                "access_ref": access_ref,
                "capabilities_ref": {"id": f"profile-{target.profile}", "version": _digest(entry)},
                "profile": target.profile,
            }
        )
    except (ValidationError, ValueError):
        raise StartRefused("TARGET_SETTINGS_INVALID") from None
    return {
        "target_selection": selection.model_dump(mode="json"),
        "target": settings,
        "env_file_ref": None,
        "template_version": "v2",
    }


def sandbox_settings(value: str | None) -> dict | None:
    """Ticket 57: the server's rehearsal shape (`SandboxSettings` JSON) or None when unset.

    Only "is a JSON object" is checked here; the worker validates the fields when it assembles
    the Sandbox input. The value is never echoed in the refusal.
    """
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        raise StartRefused("SANDBOX_SETTINGS_INVALID", 503) from None
    if not isinstance(parsed, dict):
        raise StartRefused("SANDBOX_SETTINGS_INVALID", 503)
    return parsed or None


def data_migration_settings(value: str | None, provider: str | None = None) -> dict | None:
    """Data-migration ticket 08: the server's `DataMigrationInput` JSON or None when unset.

    Same value as the CLI's `--data-migration` (source DB, source app, cutover settings, target
    tunnel address). Checked here, nested `app`/`settings` included, so a bad value refuses the
    start instead of failing after provisioning. The value is never echoed in the refusal.

    Provisioning 76 (ADR-007 2안): an ON_PREM start whose tunnel ingress is the source EKS runs
    the network preflight by default, so it needs `network_preflight`; without it the start is
    refused instead of replicating over an unchecked path."""
    if not value:
        return None
    try:
        parsed = DataMigrationInput.model_validate_json(value)
        SourceApp.model_validate(parsed.app)
        CutoverSettings.model_validate(parsed.settings)
    except (ValidationError, ValueError):
        raise StartRefused("DATA_MIGRATION_SETTINGS_INVALID", 503) from None
    reverse = parsed.tunnel is not None and parsed.tunnel.ingress == "source"
    if provider == "ON_PREM" and reverse and parsed.network_preflight is None:
        raise StartRefused("DATA_MIGRATION_PREFLIGHT_REQUIRED", 503)
    return parsed.model_dump(mode="json")


def check_target_agent(data_migration: dict | None, agent_id: str | None) -> None:
    """데이터 이전 설정이 한 Agent의 클러스터를 가리키는데(`target_agent_id`) 다른 Agent를 골랐으면 시작하지 않는다.

    데이터 이전은 서버 설정의 kubeconfig(`target_kubeconfig_ref`)로 대상 DB를 찾고, 앱은 화면에서 고른 Agent에
    깔린다. 둘이 다르면 앱과 DB가 서로 다른 클러스터에 깔려 전환이 끝까지 가지 못한다(2026-11-07, 화면 기본값
    jackal-home-01 + 설정 soulee-mac-02). `target_agent_id`가 없거나 Agent 대상이 아니면 확인하지 않는다."""
    expected = (data_migration or {}).get("target_agent_id")
    if expected and agent_id and agent_id != expected:
        raise StartRefused("DATA_TARGET_AGENT_MISMATCH", 409)


def resolve_target_egress(
    data_migration: dict | None, agent_id: str | None, store: EgressStore = EGRESS
) -> dict | None:
    """온프레 터널의 `auto_target_egress`를 시작할 때 채운다(워커에는 고정된 CIDR만 간다).

    그 대상 Agent가 서버에 접속한 출발지 IP(`agent_egress`)를 `/32`로 `target_egress_cidrs`에 더한다. 설정에
    적어 둔 CIDR은 그대로 남는다(자동 값이 없으면 그것만으로 진행하고, 둘 다 없으면 시작하지 않는다).
    자동을 켜지 않았으면 입력을 그대로 돌려준다."""
    tunnel = (data_migration or {}).get("tunnel")
    if not tunnel or not tunnel.get("auto_target_egress"):
        return data_migration
    cidrs = list(tunnel.get("target_egress_cidrs") or [])
    observed = store.get(agent_id) if agent_id else None
    if observed and f"{observed}/32" not in cidrs:
        cidrs.append(f"{observed}/32")
    if not cidrs:
        raise StartRefused("TARGET_EGRESS_UNKNOWN", 409)
    resolved = {**tunnel, "target_egress_cidrs": cidrs[-MAX_CIDRS:], "auto_target_egress": False}
    return {**data_migration, "tunnel": resolved}


def traffic_settings(value: str | None) -> dict | None:
    """#128: the server's `TrafficShiftInput` JSON or None when unset.

    Same value as the CLI's `--traffic` (relay worker queue, step policy). Checked here so a bad
    value refuses the start instead of failing at the traffic step, after provisioning. The value is
    never echoed in the refusal."""
    if not value:
        return None
    try:
        parsed = TrafficShiftInput.model_validate_json(value)
    except (ValidationError, ValueError):
        raise StartRefused("TRAFFIC_SETTINGS_INVALID", 503) from None
    return parsed.model_dump(mode="json")


def start_input(
    request: MigrationRequest,
    source: StartSource,
    planning: dict,
    env_ref: ContentRef,
    scope_fields: dict | None = None,
    sandbox: dict | None = None,
    data_migration: dict | None = None,
    traffic: dict | None = None,
) -> dict:
    """S10: the `build_start_input` shape (CLI) for a public GitHub source.

    06B: `scope_fields` (from `scope_analysis_fields`) fills the cluster, region, credential
    profile, role ARNs and the confirmed scope; without it the analysis input is as before.
    `sandbox` (ticket 57) is the server's rehearsal shape, the CLI's `--sandbox`.
    `data_migration` (data-migration ticket 08) is the CLI's `--data-migration`; without it the
    data step stays the scaffold, as before. `traffic` (#128) is the CLI's `--traffic`; without it
    the traffic step stays the scaffold, as before."""
    analysis = AnalysisInput(
        migration_id=request.project_id,
        repository=f"https://github.com/{source.full_name}.git",
        requested_commit=source.commit_sha,
        target_provider=request.target.provider if request.target else None,
        **(scope_fields or {}),
    )
    return {
        "project_id": request.project_id,
        "analysis": analysis.model_dump(mode="json"),
        "sandbox": sandbox,
        "planning": planning,
        "env_file_ref": env_ref.model_dump(mode="json"),
        **({"data_migration": data_migration} if data_migration else {}),
        **({"traffic": traffic} if traffic else {}),
    }
