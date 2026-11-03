"""Sandbox Agent runner with explicit target binding and immutable manifest reads."""

import json
import subprocess
from pathlib import Path

from cmp_contracts.agent import SandboxJob
from cmp_contracts.artifact import ArtifactRef

from cmp_providers.agent import JobStore
from cmp_providers.artifacts import verify_artifact_bytes

OWNER = "cmp.dev/test-job"


class AgentError(ValueError):
    """Diagnostics contain codes only, never kubectl output or credentials."""


def read_manifest(root: Path, ref: ArtifactRef, job: SandboxJob, phase: str) -> bytes:
    # IDs are metadata; only the validated digest is used as a filename.
    content = (root / ref.sha256).read_bytes()
    verify_artifact_bytes(ref, content)
    doc = json.loads(content)
    expected = (
        {"StatefulSet", "Service", "PersistentVolumeClaim"}
        if phase == "database"
        else {"Deployment", "Service"}
    )
    release = job.database_release if phase == "database" else job.application_release
    items = doc.get("items", [])
    if (
        doc.get("kind") != "List"
        or {item.get("kind") for item in items} != expected
        or len(items) != len(expected)
    ):
        raise AgentError("INVALID_MANIFEST")
    for item in items:
        metadata = item.get("metadata", {})
        if metadata.get("namespace") != job.namespace or metadata.get("name") != release:
            raise AgentError("MANIFEST_TARGET_MISMATCH")
        if (
            item["kind"] == "PersistentVolumeClaim"
            and item["spec"]["storageClassName"] != job.storage_class
        ):
            raise AgentError("STORAGE_MISMATCH")
    return content


def matches_desired(desired, actual):
    """API defaults may add fields; every rendered field must still match."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches_desired(value, actual[key]) for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(actual, list)
            and len(desired) == len(actual)
            and all(
                matches_desired(left, right) for left, right in zip(desired, actual, strict=True)
            )
        )
    return desired == actual


class K3sTarget:
    def __init__(self, kubeconfig: Path, context: str, cluster_uid: str):
        if not kubeconfig.is_file() or not context.startswith("k3d-cmp-"):
            raise AgentError("EXPLICIT_TEST_TARGET_REQUIRED")
        self.kubeconfig = kubeconfig
        self.context = context
        self.cluster_uid = cluster_uid

    def run(self, *args: str, content: bytes | None = None, timeout: int = 40) -> bytes:
        try:
            result = subprocess.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(self.kubeconfig),
                    "--context",
                    self.context,
                    "--request-timeout=30s",
                    *args,
                ],
                input=content,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            if result.returncode:
                raise AgentError("KUBERNETES_OPERATION_FAILED")
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise AgentError("KUBERNETES_UNAVAILABLE") from None

    def get(self, kind: str, name: str, namespace: str | None = None):
        args = ["get", kind, name, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        return json.loads(self.run(*args))

    def preflight(self, job: SandboxJob):
        uid = self.get("namespace", "kube-system")["metadata"]["uid"]
        if uid != self.cluster_uid or uid != job.cluster_uid:
            raise AgentError("CLUSTER_MISMATCH")
        ns = self.get("namespace", job.namespace)
        if (
            ns["metadata"]["uid"] != job.namespace_uid
            or ns["metadata"].get("labels", {}).get(OWNER) != job.job_id
        ):
            raise AgentError("NAMESPACE_OWNERSHIP_MISMATCH")
        storage = self.get("storageclass", job.storage_class)
        return {
            "connected": True,
            "cluster_uid": uid,
            "namespace_uid": job.namespace_uid,
            "storage_class": storage["metadata"]["name"],
            "provisioner": storage["provisioner"],
        }

    def verify(self, job: SandboxJob, root: Path):
        self.preflight(job)
        for phase, ref in [
            ("database", job.database_manifest),
            ("application", job.application_manifest),
        ]:
            rendered = json.loads(read_manifest(root, ref, job, phase))
            for item in rendered["items"]:
                live = self.get(item["kind"], item["metadata"]["name"], job.namespace)
                if not matches_desired(item["spec"], live["spec"]):
                    raise AgentError("LIVE_CONFIGURATION_MISMATCH")
        pvc = self.get("pvc", job.database_release, job.namespace)
        if pvc.get("status", {}).get("phase") != "Bound":
            raise AgentError("PVC_NOT_BOUND")
        for kind, name in [
            ("statefulset", job.database_release),
            ("deployment", job.application_release),
        ]:
            resource = self.get(kind, name, job.namespace)
            status = resource.get("status", {})
            desired = resource["spec"].get("replicas", 1)
            if (
                desired < 1
                or status.get("observedGeneration", 0) < resource["metadata"]["generation"]
                or status.get("readyReplicas", 0) != desired
                or status.get("updatedReplicas", 0) != desired
            ):
                raise AgentError("WORKLOAD_NOT_READY")
        path = (
            f"/api/v1/namespaces/{job.namespace}/services/"
            f"{job.application_release}:{job.application_port}/proxy{job.health_path}"
        )
        health = json.loads(self.run("get", "--raw", path))
        # This test app explicitly checks SQL and persistent data. Not a generic health proof.
        if health.get("database") != "ok" or health.get("persisted") != True:
            raise AgentError("APPLICATION_DATABASE_CHECK_FAILED")
        return {
            "pvc": "Bound",
            "database_ready": True,
            "application_ready": True,
            "service_database_check": True,
            "namespace": job.namespace,
        }

    def deploy(self, job: SandboxJob, root: Path):
        self.preflight(job)
        # Verify both before the first mutation, including the app manifest.
        database = read_manifest(root, job.database_manifest, job, "database")
        application = read_manifest(root, job.application_manifest, job, "application")
        self.run("apply", "-f", "-", "-n", job.namespace, content=database)
        self.run(
            "rollout",
            "status",
            f"statefulset/{job.database_release}",
            "-n",
            job.namespace,
            "--timeout=180s",
            timeout=190,
        )
        self.run("apply", "-f", "-", "-n", job.namespace, content=application)
        self.run(
            "rollout",
            "status",
            f"deployment/{job.application_release}",
            "-n",
            job.namespace,
            "--timeout=180s",
            timeout=190,
        )
        return self.verify(job, root)

    def cleanup(self, job: SandboxJob):
        self.preflight(job)
        self.run("delete", "namespace", job.namespace, "--wait=true", "--timeout=120s", timeout=130)


class SandboxAgent:
    def __init__(self, agent_id: str, store: JobStore, target: K3sTarget, artifacts: Path):
        self.agent_id, self.store, self.target, self.artifacts = agent_id, store, target, artifacts

    def submit(self, payload: dict):
        try:
            job = SandboxJob.model_validate(payload)
        except ValueError:
            raise AgentError("INVALID_JOB") from None
        if job.agent_id != self.agent_id or job.cluster_uid != self.target.cluster_uid:
            raise AgentError("JOB_TARGET_MISMATCH")
        return self.store.submit(job.model_dump(mode="json"))

    def poll_once(self):
        record = self.store.claim(self.agent_id)
        if record is None:
            return None
        job = SandboxJob.model_validate(record["payload"])
        try:
            result = self.target.deploy(job, self.artifacts)
            self.store.finish(job.job_id, "SUCCEEDED", result)
        except (ValueError, OSError, KeyError):
            # Mutation may have succeeded before an acknowledgement was lost.
            self.store.finish(job.job_id, "UNKNOWN", {"code": "INSPECTION_REQUIRED"})
        return self.store.get(job.job_id)

    def reconnect(self, job_id: str):
        record = self.store.get(job_id)
        job = SandboxJob.model_validate(record["payload"])
        if job.agent_id != self.agent_id or job.cluster_uid != self.target.cluster_uid:
            raise AgentError("JOB_TARGET_MISMATCH")
        if record["status"] in {"RUNNING", "UNKNOWN"}:
            try:
                result = self.target.verify(job, self.artifacts)
                self.store.finish(job_id, "SUCCEEDED", result)
            except (ValueError, OSError, KeyError):
                self.store.finish(job_id, "UNKNOWN", {"code": "INSPECTION_REQUIRED"})
        return self.store.get(job_id)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Poll a trusted local sandbox spool once")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--cluster-uid", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--reconnect")
    args = parser.parse_args()
    agent = SandboxAgent(
        args.agent_id,
        JobStore(args.store),
        K3sTarget(args.kubeconfig, args.context, args.cluster_uid),
        args.artifacts,
    )
    record = agent.reconnect(args.reconnect) if args.reconnect else agent.poll_once()
    print(
        json.dumps(
            {
                "status": record["status"] if record else "IDLE",
                "result": record["result"] if record else None,
            }
        )
    )


if __name__ == "__main__":
    main()
