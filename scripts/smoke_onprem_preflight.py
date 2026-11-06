"""Opt-in real K3s check for MNP-01 N3 (#51): Network Preflight run by the in-cluster Agent.

Isolated k3d only (context `k3d-cmp-*`, dedicated kubeconfig; the default kubeconfig is never
used). The platform API runs in-process over real HTTPS (throwaway CA, SQLite business DB) as in
`smoke_onprem_agent_install.py`; the Agent image must already be imported (`k3d image import`).
The real `run_network_preflight` Activity queues `NETWORK_PREFLIGHT` jobs through the business
DB, the Agent claims them over HTTPS, runs `KubernetesProbeExecutor` with its ServiceAccount
and reports the observation. Scenarios:

1. PASS against a source-role PostgreSQL in another Namespace (4 MiB read).
2. Same request again → the stored observation is reused (no new job, no new probe Pod).
3. Wrong password Secret → AUTH_FAILED (FAIL), start blocked.
4. Probe Namespace not owned by the Agent → UNKNOWN / NAMESPACE_NOT_OWNED, no Pod created.

Secret-free evidence goes to `<state-dir>/evidence.json`.
"""

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cmp_api.agent_registry import AgentJob
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.migration_events import InMemoryEmitter
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_contracts.network_preflight import NetworkPreflightRequest, NetworkPreflightResult
from cmp_providers.network_preflight import may_start_data_migration
from cmp_providers.onprem_agent import ENVIRONMENT, ClusterTarget
from cmp_worker.activities.network_preflight import (
    DbAgentPreflightTransport,
    NetworkPreflightActivities,
    choose_executor,
)
from cmp_worker.events import configure_emitter
from smoke_onprem_agent_install import Platform, make_tls, run, wait_for
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

AGENT_NS, PROBE_NS, UNOWNED_NS, SOURCE_NS = (
    "cmp-system",
    "cmp-migration",
    "cmp-unowned",
    "source-db",
)
AGENT_ID, RELEASE = "agent-preflight-smoke", "cmp-agent"
PROBE_LABEL = "app.kubernetes.io/managed-by=cmp-network-preflight"


def manifests(db_image: str, password: str) -> list[dict]:
    labels = {"app": "source-db"}
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": SOURCE_NS}},
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": PROBE_NS, "annotations": {ENVIRONMENT: "onprem-prod-001"}},
        },
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": UNOWNED_NS}},
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "source-db", "namespace": SOURCE_NS},
            "spec": {
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "postgres",
                                "image": db_image,
                                "env": [
                                    {"name": "POSTGRES_USER", "value": "replicator"},
                                    {"name": "POSTGRES_PASSWORD", "value": password},
                                    {"name": "POSTGRES_DB", "value": "demoapp"},
                                ],
                                "ports": [{"containerPort": 5432}],
                                "readinessProbe": {
                                    "exec": {"command": ["pg_isready", "-U", "replicator"]},
                                    "periodSeconds": 2,
                                },
                            }
                        ]
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "source-db", "namespace": SOURCE_NS},
            "spec": {"selector": labels, "ports": [{"port": 5432, "targetPort": 5432}]},
        },
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--agent-image", required=True, help="imported local tag, e.g. x:dev")
    parser.add_argument(
        "--probe-image",
        help="digest-pinned, e.g. postgres@sha256:; omitted: the Chart default (ticket 52, D12)",
    )
    parser.add_argument(
        "--observe-revocation",
        type=int,
        default=0,
        help="ticket 52 (C-09): revoke at the end and watch the Agent Pod for N seconds",
    )
    args = parser.parse_args()
    chart_default = yaml.safe_load((ROOT / "infra/helm/onprem-agent/values.yaml").read_text())[
        "networkPreflight"
    ]["image"]
    probe_image = args.probe_image or chart_default
    if args.kubeconfig.resolve() == (Path.home() / ".kube/config").resolve():
        raise ValueError("Dedicated kubeconfig required")
    if not args.context.startswith("k3d-cmp-"):
        raise ValueError("Isolated k3d-cmp-* test context required")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    kube = ["--kubeconfig", str(args.kubeconfig), "--context", args.context]
    helm_kube = ["--kubeconfig", str(args.kubeconfig), "--kube-context", args.context]

    def kubectl(*a, **kw):
        return run("kubectl", *kube, *a, **kw)

    host = ClusterTarget(args.kubeconfig, args.context, "pending")
    cluster_uid = host.get("namespace", "kube-system")["metadata"]["uid"]
    for namespace in (AGENT_NS, PROBE_NS, UNOWNED_NS, SOURCE_NS):
        if host.get("namespace", namespace) is not None:
            raise RuntimeError(f"{namespace} already exists; refusing to reuse it")
    node = json.loads(kubectl("get", "nodes", "-o", "json"))["items"][0]["metadata"]["name"]

    engine = create_engine(f"sqlite+pysqlite:///{args.state_dir / 'business.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)
    ca_crt, cert, key = make_tls(args.state_dir)
    admin_key = secrets.token_urlsafe(24)
    settings.agent_enrollment_admin_key = admin_key
    platform = Platform(db, LocalArtifactStore(args.state_dir / "artifacts"), cert, key)
    platform.start()
    api = httpx.Client(
        base_url=f"https://127.0.0.1:{platform.port}",
        verify=False,  # host-side test client only; the Agent verifies the CA
        headers={"X-Enrollment-Admin-Key": admin_key},
    )
    emitter = InMemoryEmitter()
    configure_emitter(emitter)
    password = secrets.token_urlsafe(18)
    transport = DbAgentPreflightTransport(db)
    activities = NetworkPreflightActivities(
        lambda req: choose_executor(req, transport, lambda: None, poll_seconds=1)
    )

    def request(secret="source-db-auth", namespace=PROBE_NS) -> NetworkPreflightRequest:
        return NetworkPreflightRequest.model_validate(
            {
                "migration_id": "migration-preflight-smoke",
                "source_db_host": f"source-db.{SOURCE_NS}.svc.cluster.local",
                "source_db_port": 5432,
                "tls_mode": "disable",
                "database": "demoapp",
                "db_auth_ref": {
                    "secret_name": secret,
                    "username_key": "username",
                    "password_key": "password",
                },
                # k3d stand-ins: Service CIDR for the AWS side, Pod CIDR for on-prem.
                "aws_vpc_cidrs": ["10.43.0.0/16"],
                "onprem_routed_cidrs": ["10.42.0.0/16"],
                "tunnel_cidrs": ["10.200.0.0/24"],
                "expected_network_path": {"allowed_private_destination_cidrs": ["10.43.0.0/16"]},
                "probe_target": {
                    "agent_id": AGENT_ID,
                    "cluster_uid": cluster_uid,
                    "namespace": namespace,
                    "node_name": node,
                },
            }
        )

    def preflight(req: NetworkPreflightRequest) -> dict:
        started = time.monotonic()
        result = asyncio.run(
            ActivityEnvironment().run(activities.run_network_preflight, req.model_dump(mode="json"))
        )
        parsed = NetworkPreflightResult.model_validate_json(json.dumps(result))
        return {
            "status": result["status"],
            "checks": {c["name"]: [c["status"], c["code"]] for c in result["checks"]},
            "transfer": next(
                c["evidence"] for c in result["checks"] if c["name"] == "probe_large_transfer"
            ),
            "probe_location": result["probe_location"],
            "may_start": may_start_data_migration(parsed, req),
            "seconds": round(time.monotonic() - started, 1),
        }

    def jobs():
        with db() as session:
            rows = session.scalars(select(AgentJob).order_by(AgentJob.id)).all()
            return [
                {"id": r.id, "status": r.status, "code": (r.result or {}).get("code")} for r in rows
            ]

    def probe_pods():
        return len(
            json.loads(kubectl("get", "pods", "-A", "-l", PROBE_LABEL, "-o", "json"))["items"]
        )

    def agent_logs():
        pods = json.loads(
            kubectl(
                "get",
                "pods",
                "-n",
                AGENT_NS,
                "-l",
                "app.kubernetes.io/name=cmp-onprem-agent",
                "-o",
                "json",
            )
        )["items"]
        out = kubectl(
            "logs", "-n", AGENT_NS, pods[0]["metadata"]["name"], "-c", "agent", check=False
        )
        return [json.loads(line) for line in out.splitlines() if line.startswith("{")]

    evidence = {"cluster": {"context": args.context, "node": node}, "scenarios": {}}
    scenarios = evidence["scenarios"]
    try:
        for document in manifests(probe_image, password):
            kubectl("apply", "-f", "-", content=json.dumps(document))
        for secret, value in (("source-db-auth", password), ("source-db-bad", "wrong-" + password)):
            kubectl("create", "secret", "generic", secret, "-n", PROBE_NS,
                    "--from-literal=username=replicator", "--from-literal=password=" + value)  # fmt: skip
        kubectl("create", "secret", "generic", "source-db-auth", "-n", UNOWNED_NS,
                "--from-literal=username=replicator", "--from-literal=password=" + password)  # fmt: skip
        kubectl("rollout", "status", "deployment/source-db", "-n", SOURCE_NS, "--timeout=180s",
                timeout=200)  # fmt: skip

        issued = api.post(
            "/agent-enrollments", json={"owner_id": "owner-smoke", "agent_id": AGENT_ID}
        )
        issued.raise_for_status()
        repository, tag = args.agent_image.rsplit(":", 1)
        values = {
            "agentId": AGENT_ID,
            "platform": {"url": f"https://host.k3d.internal:{platform.port}", "caBundle": ca_crt.read_text()},
            "enrollmentToken": issued.json()["enrollment_token"],
            "image": {"repository": repository, "tag": tag, "digest": "", "pullPolicy": "Never"},
            "idleSeconds": 2,
            "tunnel": {"enabled": False},
        }  # fmt: skip
        if args.probe_image:  # ticket 52: without it the install relies on the Chart default
            values["networkPreflight"] = {"image": args.probe_image}
        evidence["probe_image"] = {"value": probe_image, "chart_default": not args.probe_image}
        values_file = args.state_dir / "agent-values.json"
        values_file.write_text(json.dumps(values))
        values_file.chmod(0o600)
        run("helm", *helm_kube, "install", RELEASE, str(ROOT / "infra/helm/onprem-agent"),
            "-n", AGENT_NS, "--create-namespace", "-f", str(values_file),
            timeout=240)  # fmt: skip
        # No `--wait`: Helm 4 also waits on the enrollment Secret, which the Agent deletes as
        # soon as it registers (race seen on 2026-11-06); registration is awaited below.
        values_file.unlink()
        wait_for(
            lambda: (
                (r := api.get(f"/agent-admin/agents/{AGENT_ID}")).status_code == 200
                and r.json()["last_seen_at"]
            ),
            what="registration",
        )

        scenarios["pass"] = preflight(request()) | {"jobs": jobs(), "probe_pods_left": probe_pods()}
        scenarios["same_request_reused"] = preflight(request()) | {"jobs": jobs()}
        scenarios["bad_password"] = preflight(request(secret="source-db-bad")) | {"jobs": jobs()}
        scenarios["namespace_not_owned"] = preflight(request(namespace=UNOWNED_NS)) | {
            "jobs": jobs(),
            "pods_in_unowned_namespace": len(
                json.loads(kubectl("get", "pods", "-n", UNOWNED_NS, "-o", "json"))["items"]
            ),
        }
        logs = agent_logs()
        evidence["agent_log_job_events"] = [
            {k: e.get(k) for k in ("status", "code")} for e in logs if e.get("event") == "job"
        ]
        evidence["events"] = [
            {"type": e.type, "status": e.status, "result": e.data["status"], "may_start": e.data["may_start"]}
            for e in emitter.events
        ]  # fmt: skip
        evidence["probe_pods_left"] = probe_pods()
        if args.observe_revocation:
            evidence["revoked_idle"] = observe_revocation(api, kubectl, args.observe_revocation)
        dumped = json.dumps([evidence, logs, [e.data for e in emitter.events]])
        evidence["password_absent_from_evidence_logs_events"] = password not in dumped
        with db() as session:
            stored = json.dumps(
                [r.payload | {"result": r.result} for r in session.scalars(select(AgentJob))]
            )
        evidence["password_absent_from_job_rows"] = password not in stored
    finally:
        cleanup = {
            "agent_release_uninstalled": subprocess_ok(
                ["helm", *helm_kube, "uninstall", RELEASE, "-n", AGENT_NS, "--wait"]
            ),
            "namespaces_deleted": subprocess_ok(
                [
                    "kubectl",
                    *kube,
                    "delete",
                    "namespace",
                    AGENT_NS,
                    PROBE_NS,
                    UNOWNED_NS,
                    SOURCE_NS,
                    "--wait=true",
                    "--ignore-not-found",
                ]
            ),
        }
        evidence["cleanup"] = cleanup
        platform.stop()
        (args.state_dir / "evidence.json").write_text(json.dumps(evidence, indent=1, default=str))
        print(json.dumps(evidence, indent=1, default=str))


def observe_revocation(api, kubectl, seconds: int) -> dict:
    """Ticket 52 (D14): after revocation both containers stay up (no restart loop)."""
    api.post(f"/agent-admin/agents/{AGENT_ID}/revoke").raise_for_status()
    time.sleep(seconds)
    [pod] = json.loads(
        kubectl("get", "pods", "-n", AGENT_NS, "-l", "app.kubernetes.io/name=cmp-onprem-agent",
                "-o", "json")
    )["items"]  # fmt: skip
    statuses = pod["status"].get("containerStatuses", [])
    logs = kubectl("logs", "-n", AGENT_NS, pod["metadata"]["name"], "-c", "agent", check=False)
    stopped = [
        json.loads(line)
        for line in logs.splitlines()
        if line.startswith("{") and json.loads(line).get("event") == "stopped"
    ]
    return {
        "seconds": seconds,
        "phase": pod["status"].get("phase"),
        "restarts": {c["name"]: c.get("restartCount", 0) for c in statuses},
        "running": {c["name"]: "running" in (c.get("state") or {}) for c in statuses},
        "agent_stopped_events": stopped,
    }


def subprocess_ok(argv) -> bool:
    import subprocess

    return subprocess.run(argv, capture_output=True, check=False, timeout=300).returncode == 0


if __name__ == "__main__":
    main()
