"""Opt-in real K3s check for ticket 12 (approved ON_PREM rollout). Isolated k3d only.

Runs the platform API (real HTTP, SQLite business DB, local artifact store) in-process, the real
`execute_on_prem_plan` Activity and the real outbound Agent runner against an explicit
kubeconfig/context. Records secret-free evidence to `<state-dir>/evidence.json`, then deletes the
Namespace it created. Never uses or changes the default kubeconfig.
"""

import argparse
import asyncio
import json
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))  # reuse the reviewed Plan/approval fixtures builder

from cmp_api.agent_registry import AgentJob, create_enrollment
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import LocalArtifactStore, rollout_status
from cmp_api.routes.agents import get_artifact_store
from cmp_api.routes.agents import get_session as agent_session
from cmp_providers.onprem_agent import ENVIRONMENT, ClusterTarget, RolloutAgent
from cmp_providers.remote_agent import AgentControlClient
from cmp_worker.activities.onprem_execution import OnPremExecutionActivities
from onprem_rollout_support import (
    NAMESPACE,
    approve,
    build_artifacts,
    claim_request,
    plan,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment


def kubectl(target: ClusterTarget, *args, content=None):
    result = target.kubectl(*args, content=content)
    if result.returncode:
        raise RuntimeError(f"kubectl {args[0]} failed")
    return result.stdout


def helm_revision(target, release):
    info = json.loads(target.helm("status", release, "-n", NAMESPACE, "-o", "json").stdout)
    return info["version"]


def sql(target, statement: str) -> str:
    """Ticket 51: one psql statement in the DB container (credentials stay in its env)."""
    script = f'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{statement}"'
    out = kubectl(target, "exec", "-n", NAMESPACE, "statefulset/demo-db", "-c", "postgres",
                  "--", "sh", "-c", script)  # fmt: skip
    return out.decode().strip()


def workload_state(target) -> dict:
    """Ticket 51: what is running now (readiness, DB Pod, Helm history statuses only)."""
    sts = target.get("statefulset", "demo-db", NAMESPACE)
    app_ = target.get("deployment", "demo-app", NAMESPACE)
    pods = json.loads(
        kubectl(
            target,
            "get",
            "pods",
            "-n",
            NAMESPACE,
            "-l",
            "app.kubernetes.io/instance=demo-db",
            "-o",
            "json",
        )
    )["items"]
    return {
        "db_ready_replicas": sts["status"].get("readyReplicas", 0),
        "app_ready_replicas": app_["status"].get("readyReplicas", 0),
        "db_pods": [
            {
                "phase": pod["status"].get("phase"),
                "ready": any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in pod["status"].get("conditions", [])
                ),
            }
            for pod in pods
        ],
        "helm_history": {
            release: [item["status"] for item in target.history(release, NAMESPACE)]
            for release in ("demo-db", "demo-app")
        },
    }


def wait_app_ready(target, seconds=120) -> bool:
    for _ in range(seconds // 2):
        state = workload_state(target)
        if state["app_ready_replicas"] == 1 and state["db_ready_replicas"] == 1:
            return True
        time.sleep(2)
    return False


def start_api(db, store) -> str:
    def session():
        with db() as value:
            yield value

    app.dependency_overrides[agent_session] = session
    app.dependency_overrides[get_artifact_store] = lambda: store
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}"


async def execute(db, store, request, wait):
    activity = OnPremExecutionActivities(db, store, wait_seconds=wait, poll_seconds=0.5)
    return await ActivityEnvironment().run(activity.execute_on_prem_plan, request)


async def execute_with_agent(db, store, request, agent):
    """Activity waits while the Agent claims, deploys and reports in another thread."""

    def poll_until_claimed():
        for _ in range(120):  # the Activity queues the job concurrently
            result = agent.poll_once()
            if result is not None:
                return result
            time.sleep(0.5)
        return None

    agent_result = asyncio.to_thread(poll_until_claimed)
    activity, polled = await asyncio.gather(execute(db, store, request, 600), agent_result)
    return activity, polled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--application-image", required=True)
    parser.add_argument("--unpullable-application-image", required=True)
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

    probe = ClusterTarget(args.kubeconfig, args.context, "pending")
    cluster_uid = probe.get("namespace", "kube-system")["metadata"]["uid"]
    target = ClusterTarget(args.kubeconfig, args.context, cluster_uid, helm_timeout="90s")
    if probe.get("namespace", NAMESPACE) is not None:
        raise RuntimeError("Test namespace already exists; refusing to reuse it")

    engine = create_engine(f"sqlite+pysqlite:///{args.state_dir / 'business.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)
    store = LocalArtifactStore(args.state_dir / "artifacts")
    images = {
        "db_image": args.database_image,
        "app_image": args.application_image,
        "platforms": (args.platform,),
    }
    evidence = {"cluster": {"context": args.context}, "steps": {}}
    steps = evidence["steps"]
    try:
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

        artifacts = build_artifacts(store, args.state_dir, **images)
        with db() as session:
            manifest = plan(artifacts, agent_id="agent-smoke", cluster_uid=cluster_uid)
            request = claim_request(approve(session, manifest), manifest)

        # 1. Agent offline: job is preserved as QUEUED, Activity reports UNKNOWN.
        offline = asyncio.run(execute(db, store, request, 1))
        with db() as session:
            steps["offline"] = {
                "activity": offline,
                "rollout": rollout_status(session, "plan-k3s", 1),
            }

        # User-side preparation: owned Namespace and the app's user Secret (value stays local).
        kubectl(
            target,
            "apply",
            "-f",
            "-",
            content=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": NAMESPACE,
                        "annotations": {ENVIRONMENT: "onprem-prod-001"},
                    },
                }
            ).encode(),
        )
        kubectl(
            target,
            "create",
            "secret",
            "generic",
            "demo-app-env",
            "-n",
            NAMESPACE,
            "--from-literal=API_KEY=" + secrets.token_urlsafe(16),
        )

        # 2. Reconnect: Agent claims the preserved job; the retried Activity observes READY.
        activity, polled = asyncio.run(execute_with_agent(db, store, request, agent))
        steps["reconnect_ready"] = {
            "agent": polled,
            "activity": activity,
            "helm_revisions": {r: helm_revision(target, r) for r in ("demo-db", "demo-app")},
        }

        # 3. Lost result report: job forced back to RUNNING; Agent verifies without reinstalling.
        with db() as session:
            job = session.get(AgentJob, activity["resource_refs"]["job_id"])
            job.status, job.result = "RUNNING", None
            session.commit()
        recovered = agent.poll_once()
        steps["lost_report_recovery"] = {
            "agent": recovered,
            "helm_revisions": {r: helm_revision(target, r) for r in ("demo-db", "demo-app")},
            "activity": asyncio.run(execute(db, store, request, 1)),
        }

        # 4. New approved Revision whose app cannot become Ready: never READY.
        broken = build_artifacts(
            store, args.state_dir, **(images | {"app_image": args.unpullable_application_image})
        )
        with db() as session:
            manifest2 = plan(broken, revision=2, agent_id="agent-smoke", cluster_uid=cluster_uid)
            request2 = claim_request(approve(session, manifest2), manifest2)
        activity2, polled2 = asyncio.run(execute_with_agent(db, store, request2, agent))
        with db() as session:
            steps["not_ready_revision"] = {
                "agent": polled2,
                "activity": activity2,
                "rollout": rollout_status(session, "plan-k3s", 2),
            }
        # Ticket 51: the failed app Revision was rolled back; app and DB still serve rev1.
        steps["not_ready_revision"]["after"] = workload_state(target)
        steps["not_ready_revision"]["app_ready_again"] = wait_app_ready(target)

        # 5. Audit C-02: a Revision whose DB and app Pods cannot schedule on this node's
        #    architecture. The running DB (and its data) and app must survive, and the next
        #    Revision must apply without INSPECTION_REQUIRED.
        sql(
            target,
            "CREATE TABLE IF NOT EXISTS c02_probe(v text); INSERT INTO c02_probe VALUES ('kept')",
        )
        other = "linux/amd64" if args.platform == "linux/arm64" else "linux/arm64"
        wrong = build_artifacts(store, args.state_dir, **(images | {"platforms": (other,)}))
        with db() as session:
            manifest3 = plan(wrong, revision=3, agent_id="agent-smoke", cluster_uid=cluster_uid)
            request3 = claim_request(approve(session, manifest3), manifest3)
        started = time.monotonic()
        activity3, polled3 = asyncio.run(execute_with_agent(db, store, request3, agent))
        steps["c02_wrong_arch_revision"] = {
            "agent": polled3,
            "activity": activity3,
            "seconds": round(time.monotonic() - started),
            "after": workload_state(target),
            "app_ready_again": wait_app_ready(target),
            "rows_kept": sql(target, "SELECT count(*) FROM c02_probe"),
        }

        # 6. Next approved Revision (the original images) applies normally.
        fixed = build_artifacts(store, args.state_dir, **images)
        with db() as session:
            manifest4 = plan(fixed, revision=4, agent_id="agent-smoke", cluster_uid=cluster_uid)
            request4 = claim_request(approve(session, manifest4), manifest4)
        activity4, polled4 = asyncio.run(execute_with_agent(db, store, request4, agent))
        steps["c02_next_revision"] = {
            "agent": polled4,
            "activity": activity4,
            "after": workload_state(target),
            "rows_kept": sql(target, "SELECT count(*) FROM c02_probe"),
        }
    finally:
        cleanup = target.kubectl(
            "delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true", timeout=180
        )
        evidence["cleanup"] = {"namespace_deleted": cleanup.returncode == 0}
        after = default_config.read_bytes() if default_config.exists() else None
        evidence["default_kubeconfig_unchanged"] = before == after
        (args.state_dir / "evidence.json").write_text(json.dumps(evidence, indent=2))
        print(json.dumps(evidence, indent=2))
        engine.dispose()


if __name__ == "__main__":
    main()
