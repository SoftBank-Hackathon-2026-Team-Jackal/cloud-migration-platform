"""Opt-in real K3s check for ticket 52 (#187): job deadline, cancel, teardown and C-14.

Isolated k3d only (context `k3d-cmp-*`, dedicated kubeconfig). The platform API runs in-process
(SQLite business DB, local artifact store) as in `smoke_onprem_rollout.py`. The Agent runs on
the host but with the **Agent Chart's ServiceAccount token** (ClusterRole rendered from this
checkout), so the reduced RBAC (no `watch`, Namespace delete) is exercised for real.

The application image is referenced through a fake platform ECR host that the k3d node mirrors
to a local registry (`--registry-config`), so the ticket 29 pull-Secret path runs with a fake
token (no AWS). Steps:

1. rev1 READY (pull Secret owned by the rev1 job).
2. C-14: rev2 whose app cannot schedule → FAILED, app restored (51); the pull Secret goes back to
   the rev1 job and the platform still refreshes it.
3. Deadline (N14): rev3 queued while the Agent is offline → Activity `FAILED AGENT_OFFLINE`;
   the returning Agent gets nothing, Helm revisions unchanged.
4. Cancel: rev4 queued, cancelled by the operator API → never handed out.
5. Teardown (keep data): Releases, managed Secrets (but the DB password) and NetworkPolicies
   removed; PVC and Namespace kept.
6. Teardown (delete data): PVC and Namespace removed.

Secret-free evidence goes to `<state-dir>/evidence.json`. Never uses the default kubeconfig.
"""

import argparse
import asyncio
import base64
import json
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from cmp_api.agent_registry import AgentJob, create_enrollment
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import LocalArtifactStore, build_rollout_job, dispatch
from cmp_api.routes.agent_jobs import get_session as admin_session
from cmp_api.routes.agents import get_ecr_minter
from cmp_providers.agent_image_pull import ISSUED_AT, JOB_ID, PullSecretRefresher
from cmp_providers.ecr_pull import EcrTokenMinter
from cmp_providers.onprem_agent import ENVIRONMENT, ClusterTarget, RolloutAgent
from cmp_providers.remote_agent import AgentControlClient
from onprem_rollout_support import NAMESPACE, approve, build_artifacts, claim_request, plan
from smoke_onprem_rollout import execute, execute_with_agent, kubectl, start_api, workload_state
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

AGENT_NS = "cmp-system"
AGENT_SA = "cmp-onprem-agent"
TOKEN = "fake-ecr-" + secrets.token_urlsafe(12)  # never leaves this process except to the cluster


class FakeEcr:
    """Stands in for boto3 ECR: a fake token for the mirrored fake ECR host (no AWS)."""

    def __init__(self, registry: str):
        self.registry = registry

    def get_authorization_token(self):
        return {
            "authorizationData": [
                {
                    "proxyEndpoint": f"https://{self.registry}",
                    "authorizationToken": base64.b64encode(f"AWS:{TOKEN}".encode()).decode(),
                    "expiresAt": datetime.now(UTC) + timedelta(hours=12),
                }
            ]
        }


def agent_rbac(admin: ClusterTarget, kubeconfig: Path, state: Path) -> Path:
    """Apply the Chart's ServiceAccount/ClusterRole/Binding and write a token kubeconfig."""
    rendered = subprocess.run(
        ["helm", "template", "cmp-agent", str(ROOT / "infra/helm/onprem-agent"), "-n", AGENT_NS,
         "--set", "agentId=agent-smoke", "--set", "platform.url=https://platform.example",
         "--set", "image.tag=dev"],
        capture_output=True, check=True, timeout=60,
    ).stdout  # fmt: skip
    kubectl(admin, "create", "namespace", AGENT_NS)
    for doc in yaml.safe_load_all(rendered):
        if doc and doc["kind"] in {"ServiceAccount", "ClusterRole", "ClusterRoleBinding"}:
            kubectl(admin, "apply", "-f", "-", content=json.dumps(doc).encode())
    token = kubectl(admin, "create", "token", AGENT_SA, "-n", AGENT_NS, "--duration=2h")
    config = yaml.safe_load(kubeconfig.read_text())
    context = next(c for c in config["contexts"] if c["name"] == admin.context)["context"]
    cluster = next(c for c in config["clusters"] if c["name"] == context["cluster"])
    path = state / "agent-kubeconfig"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [{"name": "c", "cluster": cluster["cluster"]}],
                "users": [{"name": "agent", "user": {"token": token.decode().strip()}}],
                "contexts": [{"name": admin.context, "context": {"cluster": "c", "user": "agent"}}],
                "current-context": admin.context,
            }
        )
    )
    path.chmod(0o600)
    return path


def can_i(admin: ClusterTarget, *args) -> str:
    who = f"system:serviceaccount:{AGENT_NS}:{AGENT_SA}"
    out = admin.kubectl("auth", "can-i", *args, "--as", who)
    return out.stdout.decode().strip()


def pull_owner(admin: ClusterTarget) -> str | None:
    secret = admin.get("secret", "cmp-ecr-pull", NAMESPACE) or {}
    return ((secret.get("metadata") or {}).get("annotations") or {}).get(JOB_ID)


def names(admin: ClusterTarget, kind: str, selector: str | None = None) -> list[str]:
    args = ["get", kind, "-n", NAMESPACE, "-o", "json"] + (["-l", selector] if selector else [])
    out = admin.kubectl(*args)
    if out.returncode:
        return []
    return sorted(i["metadata"]["name"] for i in json.loads(out.stdout)["items"])


def namespace_state(admin: ClusterTarget) -> dict:
    return {
        "namespace": admin.get("namespace", NAMESPACE) is not None,
        "releases": [
            r for r in ("demo-db", "demo-app") if admin.release(r, NAMESPACE) is not None
        ] if admin.get("namespace", NAMESPACE) else [],
        "secrets": [n for n in names(admin, "secrets") if not n.startswith("sh.helm.release")],
        "networkpolicies": names(admin, "networkpolicy"),
        "pvcs": names(admin, "pvc"),
        "workloads": names(admin, "statefulset") + names(admin, "deployment"),
    }  # fmt: skip


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--application-image", required=True, help="<fake ECR host>/demo@sha256")
    parser.add_argument("--platform", choices=["linux/arm64", "linux/amd64"], required=True)
    args = parser.parse_args()
    if args.kubeconfig.resolve() == (Path.home() / ".kube/config").resolve():
        raise ValueError("Dedicated kubeconfig required")
    if not args.context.startswith("k3d-cmp-"):
        raise ValueError("Isolated k3d-cmp-* test context required")
    default_config = Path.home() / ".kube/config"
    before = default_config.read_bytes() if default_config.exists() else None
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    registry = args.application_image.split("/", 1)[0]

    probe = ClusterTarget(args.kubeconfig, args.context, "pending")
    cluster_uid = probe.get("namespace", "kube-system")["metadata"]["uid"]
    admin = ClusterTarget(args.kubeconfig, args.context, cluster_uid, helm_timeout="90s")
    for namespace in (NAMESPACE, AGENT_NS):
        if admin.get("namespace", namespace) is not None:
            raise RuntimeError(f"{namespace} already exists; refusing to reuse it")

    engine = create_engine(f"sqlite+pysqlite:///{args.state_dir / 'business.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)
    store = LocalArtifactStore(args.state_dir / "artifacts")
    settings.platform_ecr_registry = registry
    settings.platform_ecr_repositories = "demo"
    settings.agent_enrollment_admin_key = admin_key = secrets.token_urlsafe(16)
    app.dependency_overrides[get_ecr_minter] = lambda: EcrTokenMinter(lambda r: FakeEcr(registry))

    def session_override():
        with db() as value:
            yield value

    app.dependency_overrides[admin_session] = session_override
    operator = TestClient(app, headers={"X-Enrollment-Admin-Key": admin_key})
    images = {
        "db_image": args.database_image,
        "app_image": args.application_image,
        "platforms": (args.platform,),
    }
    evidence = {"cluster": {"context": args.context}, "steps": {}}
    steps = evidence["steps"]
    try:
        agent_config = agent_rbac(admin, args.kubeconfig, args.state_dir)
        evidence["agent_rbac"] = {
            "watch_secrets": can_i(admin, "watch", "secrets", "-A"),
            "delete_namespaces": can_i(admin, "delete", "namespaces"),
            "create_pods": can_i(admin, "create", "pods", "-n", NAMESPACE),
        }
        target = ClusterTarget(agent_config, args.context, cluster_uid, helm_timeout="90s")
        base_url = start_api(db, store)
        client = AgentControlClient(base_url)
        with db() as session:
            token = create_enrollment(
                session, owner_id="owner-smoke", agent_id="agent-smoke", cluster_uid=cluster_uid
            )
        credential = client.register(
            enrollment_token=token, agent_id="agent-smoke", cluster_uid=cluster_uid
        )
        agent = RolloutAgent("agent-smoke", credential, client, target)

        kubectl(admin, "apply", "-f", "-", content=json.dumps({
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": NAMESPACE, "annotations": {ENVIRONMENT: "onprem-prod-001"}},
        }).encode())  # fmt: skip
        kubectl(admin, "create", "secret", "generic", "demo-app-env", "-n", NAMESPACE,
                "--from-literal=API_KEY=" + secrets.token_urlsafe(16))  # fmt: skip

        def revision(number, **override):
            built = build_artifacts(store, args.state_dir, **(images | override))
            with db() as session:
                manifest = plan(built, revision=number, agent_id="agent-smoke",
                                cluster_uid=cluster_uid)  # fmt: skip
                return claim_request(approve(session, manifest), manifest)

        # 1. rev1 READY with the ECR pull Secret (Agent ServiceAccount permissions only).
        request1 = revision(1)
        activity1, polled1 = asyncio.run(execute_with_agent(db, store, request1, agent))
        job1 = activity1["resource_refs"]["job_id"]
        steps["rev1_ready"] = {
            "agent": polled1,
            "activity_status": [activity1["status"], activity1["code"]],
            "pull_secret_owner_is_rev1": pull_owner(admin) == job1,
            "state": namespace_state(admin),
        }

        # 2. C-14: rev2's app cannot schedule (other architecture) → restored; owner back.
        other = "linux/amd64" if args.platform == "linux/arm64" else "linux/arm64"
        request2 = revision(2, platforms=(other,))
        activity2, polled2 = asyncio.run(execute_with_agent(db, store, request2, agent))
        kubectl(
            admin,
            "annotate",
            "secret",
            "cmp-ecr-pull",
            "-n",
            NAMESPACE,
            f"{ISSUED_AT}=1",
            "--overwrite",
        )  # fmt: skip  (force the half-TTL refresh)
        refresher = PullSecretRefresher(
            target,
            lambda job_id: client.image_pull(
                agent_id="agent-smoke", credential=credential, job_id=job_id
            ),
        )
        steps["c14_failed_revision"] = {
            "agent": polled2,
            "activity_status": [activity2["status"], activity2["code"]],
            "pull_secret_owner_is_rev1": pull_owner(admin) == job1,
            "refresh": refresher.refresh(),
            "after": workload_state(admin),
        }

        # 3. Deadline: rev3 queued while the Agent is offline; the returning Agent gets nothing.
        request3 = revision(3)
        offline = asyncio.run(execute(db, store, request3, 2))
        helm_before = workload_state(admin)["helm_history"]
        steps["deadline_offline"] = {
            "activity_status": [offline["status"], offline["code"]],
            "agent_poll_after_return": agent.poll_once(),
            "helm_history_unchanged": workload_state(admin)["helm_history"] == helm_before,
        }
        with db() as session:
            row = session.get(AgentJob, offline["resource_refs"]["job_id"])
            steps["deadline_offline"]["job"] = [row.status, (row.result or {}).get("code")]

        # 4. Cancel: rev4 queued for the Agent, cancelled by the operator before it is claimed.
        request4 = revision(4)
        with db() as session:
            job4, owner = build_rollout_job(session, request4, store)
            dispatch(session, job4, owner)
        cancelled = operator.post(f"/agent-admin/jobs/{job4.job_id}/cancel").json()
        steps["cancel_queued"] = {
            "cancel": [cancelled["status"], cancelled["code"]],
            "agent_poll": agent.poll_once(),
            "helm_history_unchanged": workload_state(admin)["helm_history"] == helm_before,
        }

        # 5. Teardown keeping the data. This Plan fixture has no ticket 26 policies, so seed one
        #    managed (platform) and one user NetworkPolicy to show only the managed one goes.
        for name, labels in (
            ("cmp-managed-seed", {"cmp.dev/managed": "true"}),
            ("user-policy", {}),
        ):
            kubectl(admin, "apply", "-f", "-", content=json.dumps({
                "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
                "spec": {"podSelector": {"matchLabels": {"seed": name}}, "policyTypes": ["Ingress"]},
            }).encode())  # fmt: skip
        before_teardown = namespace_state(admin)
        keep = operator.post(f"/agent-admin/jobs/{job1}/teardown", json={"delete_data": False})
        started = time.monotonic()
        steps["teardown_keep_data"] = {
            "request": [keep.status_code, keep.json()["status"]],
            "before": before_teardown,
            "agent": agent.poll_once(),
            "seconds": round(time.monotonic() - started, 1),
            "after": namespace_state(admin),
            "job": operator.get(f"/agent-admin/jobs/{keep.json()['job_id']}").json(),
        }

        # 6. Teardown deleting the data (PVC and Namespace).
        wipe = operator.post(f"/agent-admin/jobs/{job1}/teardown", json={"delete_data": True})
        started = time.monotonic()
        steps["teardown_delete_data"] = {
            "request": [wipe.status_code, wipe.json()["status"]],
            "agent": agent.poll_once(),
            "seconds": round(time.monotonic() - started, 1),
            "after": namespace_state(admin),
        }
    finally:
        cleanup = {
            ns: admin.kubectl(
                "delete", "namespace", ns, "--ignore-not-found", "--wait=true", timeout=180
            ).returncode == 0
            for ns in (NAMESPACE, AGENT_NS)
        }  # fmt: skip
        for kind in ("clusterrolebinding", "clusterrole"):
            admin.kubectl("delete", kind, f"cmp-onprem-agent-{AGENT_NS}", "--ignore-not-found")
        evidence["cleanup"] = cleanup
        after = default_config.read_bytes() if default_config.exists() else None
        evidence["default_kubeconfig_unchanged"] = before == after
        text = json.dumps(evidence, indent=2, default=str)
        evidence["fake_token_absent_from_evidence"] = TOKEN not in text
        (args.state_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str))
        print(json.dumps(evidence, indent=2, default=str))
        app.dependency_overrides.clear()
        engine.dispose()


if __name__ == "__main__":
    main()
