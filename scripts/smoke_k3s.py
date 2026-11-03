"""Opt-in real K3s smoke test. Explicit target only; never uses current context."""

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import yaml
from cmp_contracts.agent import SandboxJob
from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.chart import PostgresChartValues
from cmp_providers.agent import JobStore
from cmp_providers.app_renderer import render_application
from cmp_providers.application import resolve_application
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes
from cmp_providers.k3s_agent import OWNER, K3sTarget, SandboxAgent

ROOT = Path(__file__).resolve().parents[1]


def artifact(root, name, raw):
    data = canonical_json_bytes(
        {"apiVersion": "v1", "kind": "List", "items": [d for d in yaml.safe_load_all(raw) if d]}
    )
    digest = sha256_bytes(data)
    (root / digest).write_bytes(data)
    return ArtifactRef(id=name, version="1", kind="MANIFEST", sha256=digest, size_bytes=len(data))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--application-image", required=True)
    parser.add_argument("--platform", choices=["linux/arm64", "linux/amd64"], required=True)
    args = parser.parse_args()
    # Refuse default kubeconfig, even if its current context happens to be a test context.
    if args.kubeconfig.resolve() == (Path.home() / ".kube/config").resolve():
        raise ValueError("Dedicated kubeconfig required")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    artifacts = args.state_dir / "artifacts"
    artifacts.mkdir(exist_ok=True)
    default_config = Path.home() / ".kube/config"
    before = default_config.read_bytes() if default_config.exists() else None
    target = K3sTarget(args.kubeconfig, args.context, "pending")
    target.cluster_uid = target.get("namespace", "kube-system")["metadata"]["uid"]
    job_id = "smoke-" + secrets.token_hex(4)
    namespace = "cmp-test-" + job_id
    ns = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace, "labels": {OWNER: job_id}},
    }
    target.run("create", "-f", "-", content=canonical_json_bytes(ns))
    job = None
    evidence = {"context": args.context, "namespace": namespace, "job_id": job_id}
    try:
        ns_uid = target.get("namespace", namespace)["metadata"]["uid"]
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "db-auth", "namespace": namespace},
            "type": "Opaque",
            "stringData": {"password": secrets.token_urlsafe(32)},
        }
        target.run("create", "-f", "-", content=canonical_json_bytes(secret))
        del secret
        db = json.loads((ROOT / "tests/fixtures/contracts/postgres-values.json").read_text())
        db.update(namespace=namespace, release_name="test-db")
        db["image"] = {"uri": args.database_image, "platforms": [args.platform]}
        db["storage"]["size_gib"] = 1
        db["database"]["secret_name"] = "db-auth"
        PostgresChartValues.model_validate(db)
        db_path = args.state_dir / "db-values.json"
        db_path.write_bytes(canonical_json_bytes(db))
        helm = os.environ.get("HELM_BIN", "helm")
        rendered = subprocess.run(
            [
                helm,
                "template",
                "test-db",
                str(ROOT / "infra/helm/postgres"),
                "-n",
                namespace,
                "-f",
                str(db_path),
            ],
            capture_output=True,
            check=True,
        )
        db_ref = artifact(artifacts, "database", rendered.stdout)
        app = json.loads((ROOT / "tests/fixtures/contracts/app-values.json").read_text())
        app.update(namespace=namespace, release_name="test-app")
        app["application"]["replicas"] = 1
        app["image"] = {"uri": args.application_image, "platforms": [args.platform]}
        app["env"] = [e for e in app["env"] if e["source"]["kind"] == "DATABASE"]
        app["required_env"] = [e["name"] for e in app["env"]]
        values = resolve_application(
            app, {"DATABASE_HOST": f"test-db.{namespace}.svc", "DATABASE_SECRET_NAME": "db-auth"}
        )
        app_ref = artifact(
            artifacts,
            "application",
            render_application(values, chart_dir=ROOT / "infra/helm/application", helm_bin=helm),
        )
        job = SandboxJob(
            mode="SANDBOX_TEST",
            job_id=job_id,
            agent_id="local-agent",
            cluster_uid=target.cluster_uid,
            namespace=namespace,
            namespace_uid=ns_uid,
            storage_class="local-path",
            database_release="test-db",
            application_release="test-app",
            database_manifest=db_ref,
            application_manifest=app_ref,
            health_path="/health",
            application_port=8080,
        )
        store_path = args.state_dir / "jobs.sqlite"
        store = JobStore(store_path)
        agent = SandboxAgent("local-agent", store, target, artifacts)
        evidence["preflight"] = target.preflight(job)
        agent.submit(job.model_dump(mode="json"))
        command = [
            sys.executable,
            "-m",
            "cmp_providers.k3s_agent",
            "--store",
            str(store_path),
            "--artifacts",
            str(artifacts),
            "--kubeconfig",
            str(args.kubeconfig),
            "--context",
            args.context,
            "--cluster-uid",
            target.cluster_uid,
            "--agent-id",
            "local-agent",
        ]
        completed = subprocess.run(command, capture_output=True, check=True, timeout=450)
        evidence["deployment"] = json.loads(completed.stdout)
        if evidence["deployment"]["status"] != "SUCCEEDED":
            raise RuntimeError("Sandbox deployment did not succeed")
        uid_before = target.get("pod", "test-db-0", namespace)["metadata"]["uid"]
        assert agent.submit(job.model_dump(mode="json"))["status"] == "SUCCEEDED"
        idle = subprocess.run(command, capture_output=True, check=True, timeout=40)
        assert json.loads(idle.stdout)["status"] == "IDLE"
        reconnected = subprocess.run(
            command + ["--reconnect", job_id], capture_output=True, check=True, timeout=40
        )
        assert json.loads(reconnected.stdout)["status"] == "SUCCEEDED"
        assert target.get("pod", "test-db-0", namespace)["metadata"]["uid"] == uid_before
        evidence["duplicate_and_process_reconnect"] = "PASSED"
        # Fault injection: durable completion acknowledgement was lost after Kubernetes succeeded.
        with store.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='RUNNING', result=NULL WHERE id=?", (job_id,)
            )
        recovered = subprocess.run(
            command + ["--reconnect", job_id], capture_output=True, check=True, timeout=60
        )
        assert json.loads(recovered.stdout)["status"] == "SUCCEEDED"
        assert target.get("pod", "test-db-0", namespace)["metadata"]["uid"] == uid_before
        evidence["lost_completion_ack_recovered_without_redeploy"] = True
        evidence["live_recheck"] = target.verify(job, artifacts)
    finally:
        if job:
            target.cleanup(job)
        else:
            current = target.get("namespace", namespace)
            if current["metadata"].get("labels", {}).get(OWNER) == job_id:
                target.run(
                    "delete", "namespace", namespace, "--wait=true", "--timeout=120s", timeout=130
                )
        evidence["namespace_deleted"] = True
        after = default_config.read_bytes() if default_config.exists() else None
        evidence["default_kubeconfig_unchanged"] = before == after
        (args.state_dir / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))
        if before != after:
            raise RuntimeError("Default kubeconfig changed")


if __name__ == "__main__":
    main()
