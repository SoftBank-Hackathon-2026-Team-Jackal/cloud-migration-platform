"""Opt-in real K3s check for ticket 15 (#69): install the Agent Chart and run it in-cluster.

Isolated k3d only. The platform API runs in-process over real HTTPS (throwaway CA, SQLite
business DB, local artifact store) on 0.0.0.0 so Pods reach it as `host.k3d.internal`. The
Agent image must already be imported into the cluster (`k3d image import`). Steps:

1. Helm install with an enrollment token issued *without* a cluster UID → the Agent registers
   with the kube-system UID it discovers, stores its credential, deletes the token Secret.
2. An approved ON_PREM rollout is executed by the real Activity and the in-cluster Agent → READY.
3. Agent Pod deleted → new Pod reuses the credential (no re-registration); a lost result report
   is recovered by verification without reinstalling (Helm revision unchanged).
4. Platform outage → Agent backs off and resumes contact after the API returns.
5. Operator revocation → the Agent stops with AGENT_UNAUTHORIZED.

Secret-free evidence goes to `<state-dir>/evidence.json`. Never touches the default kubeconfig.
"""

import argparse
import asyncio
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))  # reuse the reviewed Plan/approval fixture builder

from cmp_api.agent_registry import AgentJob
from cmp_api.config import settings
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_api.routes.agents import get_artifact_store
from cmp_api.routes.agents import get_session as agent_session
from cmp_providers.onprem_agent import ENVIRONMENT, ClusterTarget
from cmp_worker.activities.onprem_execution import OnPremExecutionActivities
from onprem_rollout_support import NAMESPACE, approve, build_artifacts, claim_request, plan
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

AGENT_NS = "cmp-system"
AGENT_ID = "agent-install-smoke"
RELEASE = "cmp-agent"


def run(*argv, check=True, timeout=120, content=None) -> str:
    result = subprocess.run(
        argv, input=content, capture_output=True, check=False, timeout=timeout, text=True
    )
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} {argv[1]} failed: {result.stderr[-400:]}")
    return result.stdout


def make_tls(directory: Path) -> tuple[Path, Path, Path]:
    ca_key, ca_crt = directory / "ca.key", directory / "ca.crt"
    key, csr, crt = directory / "server.key", directory / "server.csr", directory / "server.crt"
    ext = directory / "server.ext"
    ext.write_text("subjectAltName=DNS:host.k3d.internal\nextendedKeyUsage=serverAuth\n")
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_crt),
        "-subj",
        "/CN=cmp-smoke-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    run(
        "openssl",
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(csr),
        "-subj",
        "/CN=host.k3d.internal",
    )
    run(
        "openssl",
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca_crt),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(crt),
        "-days",
        "1",
        "-extfile",
        str(ext),
    )
    for path in (ca_key, key):
        path.chmod(0o600)
    return ca_crt, crt, key


class Platform:
    def __init__(self, db, store, cert: Path, key: Path):
        def session():
            with db() as value:
                yield value

        app.dependency_overrides[agent_session] = session
        app.dependency_overrides[get_artifact_store] = lambda: store
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", 0))
            self.port = probe.getsockname()[1]
        self.cert, self.key, self.server = cert, key, None

    def start(self):
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            ssl_certfile=str(self.cert),
            ssl_keyfile=str(self.key),
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=30)


def wait_for(predicate, timeout=180, interval=2.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(what)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--agent-image", required=True, help="imported local tag, e.g. x:dev")
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--application-image", required=True)
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

    kube = ["--kubeconfig", str(args.kubeconfig), "--context", args.context]
    helm_kube = ["--kubeconfig", str(args.kubeconfig), "--kube-context", args.context]

    def kubectl(*a, **kw):
        return run("kubectl", *kube, *a, **kw)

    host = ClusterTarget(args.kubeconfig, args.context, "pending")
    cluster_uid = host.get("namespace", "kube-system")["metadata"]["uid"]
    for namespace in (AGENT_NS, NAMESPACE):
        if host.get("namespace", namespace) is not None:
            raise RuntimeError(f"{namespace} already exists; refusing to reuse it")

    engine = create_engine(f"sqlite+pysqlite:///{args.state_dir / 'business.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)
    store = LocalArtifactStore(args.state_dir / "artifacts")
    ca_crt, cert, key = make_tls(args.state_dir)
    admin_key = secrets.token_urlsafe(24)
    settings.agent_enrollment_admin_key = admin_key
    platform = Platform(db, store, cert, key)
    platform.start()
    api = httpx.Client(
        base_url=f"https://127.0.0.1:{platform.port}",
        verify=False,  # host-side test client only; the Agent verifies the CA
        headers={"X-Enrollment-Admin-Key": admin_key},
    )

    def agent_status():
        response = api.get(f"/agent-admin/agents/{AGENT_ID}")
        return response.json() if response.status_code == 200 else None

    def agent_pod():
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
        return pods[0] if pods else None

    def logs(previous=False):
        pod = agent_pod()
        argv = ["logs", "-n", AGENT_NS, pod["metadata"]["name"]]
        if previous:
            argv.append("--previous")
        lines = kubectl(*argv, check=False).splitlines()
        return [json.loads(line) for line in lines if line.startswith("{")]

    def secret_names():
        items = json.loads(kubectl("get", "secrets", "-n", AGENT_NS, "-o", "json"))["items"]
        return sorted(item["metadata"]["name"] for item in items)

    def helm_revision(release):
        out = run("helm", *helm_kube, "status", release, "-n", NAMESPACE, "-o", "json")
        return json.loads(out)["version"]

    async def execute(request, wait):
        activity = OnPremExecutionActivities(db, store, wait_seconds=wait, poll_seconds=1)
        return await ActivityEnvironment().run(activity.execute_on_prem_plan, request)

    evidence = {"cluster": {"context": args.context}, "steps": {}}
    steps = evidence["steps"]
    try:
        # 1. Install and register (enrollment issued without a cluster UID, H3).
        issued = api.post(
            "/agent-enrollments", json={"owner_id": "owner-smoke", "agent_id": AGENT_ID}
        )
        issued.raise_for_status()
        token = issued.json()["enrollment_token"]
        repository, tag = args.agent_image.rsplit(":", 1)
        values = {
            "agentId": AGENT_ID,
            "platform": {
                "url": f"https://host.k3d.internal:{platform.port}",
                "caBundle": ca_crt.read_text(),
            },
            "enrollmentToken": token,
            "image": {"repository": repository, "tag": tag, "digest": "", "pullPolicy": "Never"},
            "idleSeconds": 2,
            "helmTimeout": "120s",
        }
        values_file = args.state_dir / "agent-values.json"
        values_file.write_text(json.dumps(values))
        values_file.chmod(0o600)
        run(
            "helm",
            *helm_kube,
            "install",
            RELEASE,
            str(ROOT / "infra/helm/onprem-agent"),
            "-n",
            AGENT_NS,
            "--create-namespace",
            "-f",
            str(values_file),
            "--wait",
            "--timeout",
            "180s",
            timeout=240,
        )
        values_file.unlink()
        registered = wait_for(
            lambda: (s := agent_status()) and s["last_seen_at"] and s, what="registration"
        )
        steps["install_register"] = {
            "agent": registered,
            "registered_uid_matches_kube_system": registered["cluster_uid"] == cluster_uid,
            "secrets_in_agent_namespace": secret_names(),
            "agent_log_events": [e["event"] for e in logs()],
        }

        # User-side preparation as in ticket 12: owned Namespace and the app's user Secret.
        kubectl(
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
            ),
        )
        kubectl(
            "create",
            "secret",
            "generic",
            "demo-app-env",
            "-n",
            NAMESPACE,
            "--from-literal=API_KEY=" + secrets.token_urlsafe(16),
        )

        # 2. Approved rollout executed by the in-cluster Agent.
        images = {
            "db_image": args.database_image,
            "app_image": args.application_image,
            "platforms": (args.platform,),
        }
        artifacts = build_artifacts(store, args.state_dir, **images)
        with db() as session:
            manifest = plan(artifacts, agent_id=AGENT_ID, cluster_uid=cluster_uid)
            request = claim_request(approve(session, manifest), manifest)
        activity = asyncio.run(execute(request, 600))
        steps["rollout_ready"] = {
            "activity": activity,
            "helm_revisions": {r: helm_revision(r) for r in ("demo-db", "demo-app")},
        }

        # 3. Pod restart reuses the credential; a lost report is recovered by verification.
        old = agent_pod()["metadata"]["name"]
        with db() as session:
            job = session.get(AgentJob, activity["resource_refs"]["job_id"])
            job.status, job.result = "RUNNING", None
            session.commit()
        kubectl("delete", "pod", "-n", AGENT_NS, old, "--wait=true", timeout=180)
        wait_for(
            lambda: (
                (p := agent_pod())
                and p["metadata"]["name"] != old
                and p["status"].get("phase") == "Running"
            ),
            what="new agent pod",
        )

        def recovered():
            with db() as session:
                row = session.get(AgentJob, activity["resource_refs"]["job_id"])
                return row.status == "SUCCEEDED" and row.result

        result = wait_for(recovered, timeout=300, what="lost report recovery")
        steps["restart_and_lost_report"] = {
            "job_result": result,
            "new_pod_log_events": [e["event"] for e in logs()],
            "re_registered": any(e["event"] == "registered" for e in logs()),
            "helm_revisions": {r: helm_revision(r) for r in ("demo-db", "demo-app")},
            "activity": asyncio.run(execute(request, 5)),
        }

        # 4. Platform outage: Agent backs off, then resumes contact.
        platform.stop()
        time.sleep(12)
        retries = [e for e in logs() if e["event"] == "retry"]
        platform.start()
        seen_before = int(time.time())
        resumed = wait_for(
            lambda: (s := agent_status()) and s["last_seen_at"] >= seen_before and s,
            timeout=120,
            what="contact after outage",
        )
        steps["platform_outage"] = {
            "retry_events": [{"code": e["code"], "seconds": e["seconds"]} for e in retries],
            "resumed_last_seen_at": resumed["last_seen_at"],
            "pod_restarts": agent_pod()["status"]["containerStatuses"][0]["restartCount"],
        }

        # 5. Revocation stops the Agent with AGENT_UNAUTHORIZED.
        api.post(f"/agent-admin/agents/{AGENT_ID}/revoke").raise_for_status()

        def terminated():
            status = agent_pod()["status"]["containerStatuses"][0]
            last = status.get("lastState", {}).get("terminated") or status.get("state", {}).get(
                "terminated"
            )
            return last

        last = wait_for(terminated, timeout=120, what="agent exit after revocation")
        stop_logs = logs(previous=True) or logs()
        steps["revoked"] = {
            "exit_code": last.get("exitCode"),
            "stop_event": next((e for e in stop_logs if e["event"] == "stopped"), None),
            "admin_status": agent_status(),
        }
    finally:
        cleanup = {}
        cleanup["agent_release_uninstalled"] = (
            subprocess.run(
                ["helm", *helm_kube, "uninstall", RELEASE, "-n", AGENT_NS, "--wait"],
                check=False,
                capture_output=True,
                timeout=180,
            ).returncode
            == 0
        )
        for namespace in (NAMESPACE, AGENT_NS):
            cleanup[f"namespace_{namespace}_deleted"] = (
                subprocess.run(
                    [
                        "kubectl",
                        *kube,
                        "delete",
                        "namespace",
                        namespace,
                        "--ignore-not-found",
                        "--wait=true",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=240,
                ).returncode
                == 0
            )
        evidence["cleanup"] = cleanup
        after = default_config.read_bytes() if default_config.exists() else None
        evidence["default_kubeconfig_unchanged"] = before == after
        text = json.dumps(evidence, indent=2)
        assert token not in text and admin_key not in text
        (args.state_dir / "evidence.json").write_text(text)
        print(text)
        if platform.server:
            platform.server.should_exit = True
        engine.dispose()


if __name__ == "__main__":
    main()
