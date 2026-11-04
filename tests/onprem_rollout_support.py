"""Builds a real approved ON_PREM Plan Revision with 04B-rendered Values and packaged Charts."""

import subprocess
from pathlib import Path

from cmp_api.agent_registry import create_enrollment, redeem_enrollment
from cmp_api.approvals import (
    PlanApproval,
    authenticate_approver,
    publish_plan_revision,
    record_decision,
    register_approver,
)
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_contracts.plan import PlanManifest, Preview
from cmp_providers.artifacts import canonical_json_bytes, sha256_json
from cmp_providers.plans import manifest_digest
from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2

ROOT = Path(__file__).resolve().parents[1]
DB_CHART = ROOT / "infra/helm/postgres"
APP_CHART = ROOT / "infra/helm/application"
NAMESPACE = "cmp-prod-demo"


def render_request(
    *,
    namespace=NAMESPACE,
    storage_class="local-path",
    db_image="docker.io/library/postgres@sha256:" + "b" * 64,
    app_image="registry.example.com/demo@sha256:" + "a" * 64,
    platforms=("linux/arm64",),
    health_path="/health",
    port=8080,
    user_secret=True,
):
    env = {
        "DB_HOST": {"resource_id": "postgres-main", "output": "host"},
        "DB_PORT": {"resource_id": "postgres-main", "output": "port"},
        "DB_NAME": {"resource_id": "postgres-main", "output": "name"},
        "DB_USER": {"resource_id": "postgres-main", "output": "username"},
        "DB_PASSWORD": {"resource_id": "postgres-main", "output": "password_secret_ref"},
    }
    if user_secret:
        env["API_KEY"] = {"secret_ref": {"name": "demo-app-env", "key": "API_KEY"}}
    return {
        "contract_version": "2",
        "environment": "PRODUCTION",
        "provider": "ON_PREM",
        "namespace": namespace,
        "resources": [
            {
                "id": "postgres-main",
                "kind": "POSTGRESQL",
                "release_name": "demo-db",
                "image": {"uri": db_image, "platforms": list(platforms)},
                "version": "16",
                "extensions": [],
                "database": {
                    "name": "appdb",
                    "username": "appuser",
                    "password_secret_ref": {"name": "demo-db-auth", "key": "password"},
                },
                "resources": {"cpu_millicores": 250, "memory_mib": 512},
                "storage": {"class_name": storage_class, "size_gib": 1},
            },
            {
                "id": "api-main",
                "kind": "APPLICATION",
                "release_name": "demo-app",
                "image": {"uri": app_image, "platforms": list(platforms)},
                "replicas": 1,
                "resources": {"cpu_millicores": 100, "memory_mib": 128},
                "port": port,
                "health_path": health_path,
                "env_bindings": env,
            },
        ],
    }


def package_chart(chart_dir: Path, out: Path) -> bytes:
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["helm", "package", str(chart_dir), "--destination", str(out)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    path = Path(result.stdout.decode().strip().rsplit(" ", 1)[-1])
    return path.read_bytes()


def build_artifacts(store: LocalArtifactStore, tmp: Path, **request) -> dict:
    """Stores exact bytes and returns ArtifactRef dicts keyed by PlanManifest role."""
    rendered = render_shared_deployment_v2(
        render_request(**request), database_chart_dir=DB_CHART, application_chart_dir=APP_CHART
    )
    db = next(r for r in rendered.resources if r.kind == "POSTGRESQL")
    app = next(r for r in rendered.resources if r.kind == "APPLICATION")
    contents = {
        "spec": ("SPEC", canonical_json_bytes(render_request(**request))),
        "database_chart": ("CHART", package_chart(DB_CHART, tmp / "charts")),
        "database_values": ("VALUES", canonical_json_bytes(db.helm_values.model_dump(mode="json"))),
        "application_chart": ("CHART", package_chart(APP_CHART, tmp / "charts")),
        "application_values": (
            "VALUES",
            canonical_json_bytes(app.helm_values.model_dump(mode="json")),
        ),
        "bindings": ("BINDINGS", canonical_json_bytes({"bindings": []})),
    }
    return {
        role: store.put(content, ref_id=f"{role}-1", kind=kind)
        for role, (kind, content) in contents.items()
    }


def plan(artifacts: dict, *, revision=1, agent_id="agent-a", cluster_uid="cluster-a", **target):
    return PlanManifest.model_validate(
        {
            "contract_version": "1",
            "digest_format": "cmp-json-v1",
            "migration_id": "migration-001",
            "plan": {"plan_id": "plan-k3s", "revision": revision, "template_version": "v1"},
            "scope": "WORKLOADS",
            "target": {
                "provider": "ON_PREM",
                "environment_id": "onprem-prod-001",
                "cluster_ref": {"id": cluster_uid, "version": "1"},
                "agent_id": agent_id,
                "namespace": target.get("namespace", NAMESPACE),
            },
            "image_uri": "registry.example.com/demo@sha256:" + "a" * 64,
            "artifacts": artifacts,
        }
    )


def preview(manifest: PlanManifest) -> Preview:
    return Preview(
        contract_version="1",
        manifest_digest=manifest_digest(manifest),
        changes=[
            {"resource_type": "DATABASE", "action": "CREATE", "name": "demo-db"},
            {"resource_type": "APP", "action": "CREATE", "name": "demo-app"},
        ],
    )


def register_agent(session, agent_id="agent-a", cluster_uid="cluster-a", owner="owner-a") -> str:
    token = create_enrollment(session, owner_id=owner, agent_id=agent_id, cluster_uid=cluster_uid)
    return redeem_enrollment(session, token=token, agent_id=agent_id, cluster_uid=cluster_uid)


def approve(session, manifest: PlanManifest, credential: str | None = None) -> PlanApproval:
    display = preview(manifest)
    publish_plan_revision(session, manifest, display)
    credential = credential or register_approver(
        session, approver_id=f"approver-{manifest.plan.revision}", migration_ids=["migration-001"]
    )
    record, _ = record_decision(
        session,
        approver=authenticate_approver(session, credential),
        plan_id=manifest.plan.plan_id,
        revision=manifest.plan.revision,
        decision="APPROVE",
        scope=manifest.scope,
        manifest_digest=manifest_digest(manifest),
        preview_digest=sha256_json(display.model_dump(mode="json")),
    )
    return record


def claim_request(record: PlanApproval, manifest: PlanManifest) -> dict:
    """Fields returned by 08's `claim_approved_execution` (dispatch owned by 08)."""
    return {
        "status": "CLAIMED",
        "code": "EXECUTION_CLAIMED",
        "approval_id": record.id,
        "plan_id": record.plan_id,
        "revision": record.revision,
        "provider": "ON_PREM",
        "scope": record.scope,
        "manifest_digest": manifest_digest(manifest),
    }
