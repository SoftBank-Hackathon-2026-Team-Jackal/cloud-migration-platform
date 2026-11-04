"""Approved ON_PREM rollout Agent runner (ticket 12, E4/E6/E7/E8/E9).

The Agent polls the platform outbound, receives one `PRODUCTION_ROLLOUT` job, downloads only that
job's Chart/Values bytes and verifies their digests, then installs/upgrades two Helm Releases in
the user's K3s with an explicit kubeconfig/context. Before any mutation it inspects the cluster
UID, Namespace ownership, StorageClass, Secrets and existing Releases, so a reclaimed job (lost
report or reconnect) is verified instead of silently re-applied. Diagnostics are codes only;
kubectl/helm output, Values and Secret values are never reported.
"""

import argparse
import base64
import json
import secrets
import subprocess
import tempfile
from pathlib import Path

from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.onprem_rollout import ProductionRolloutJob
from pydantic import ValidationError

from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.remote_agent import AgentControlClient, AgentTransportError, _secret_file

MANAGED = "cmp.dev/managed"
ENVIRONMENT = "cmp.dev/environment-id"


class RolloutAgentError(Exception):
    """`status` is FAILED (definitive check) or UNKNOWN (inspection required)."""

    def __init__(self, code: str, status: str = "FAILED"):
        super().__init__(code)
        self.code = code
        self.status = status


class ClusterUnavailable(RolloutAgentError):
    def __init__(self):
        super().__init__("KUBERNETES_UNAVAILABLE", "UNKNOWN")


class ClusterTarget:
    """Every kubectl/helm call uses the dedicated kubeconfig and explicit context."""

    def __init__(self, kubeconfig: Path, context: str, cluster_uid: str, helm_timeout: str = "5m"):
        if not Path(kubeconfig).is_file() or not context:
            raise RolloutAgentError("EXPLICIT_TARGET_REQUIRED")
        self.kubeconfig, self.context = Path(kubeconfig), context
        self.cluster_uid, self.helm_timeout = cluster_uid, helm_timeout

    def _exec(self, argv: list[str], content: bytes | None, timeout: int):
        try:
            return subprocess.run(
                argv, input=content, capture_output=True, check=False, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError):
            raise ClusterUnavailable() from None

    def kubectl(self, *args: str, content: bytes | None = None, timeout: int = 40):
        return self._exec(
            [
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "--context",
                self.context,
                "--request-timeout=30s",
                *args,
            ],
            content,
            timeout,
        )

    def helm(self, *args: str, timeout: int = 60):
        return self._exec(
            [
                "helm",
                "--kubeconfig",
                str(self.kubeconfig),
                "--kube-context",
                self.context,
                *args,
            ],
            None,
            timeout,
        )

    def get(self, kind: str, name: str, namespace: str | None = None) -> dict | None:
        args = ["get", kind, name, "-o", "json", "--ignore-not-found"]
        if namespace:
            args += ["-n", namespace]
        result = self.kubectl(*args)
        if result.returncode:
            raise ClusterUnavailable()
        return json.loads(result.stdout) if result.stdout.strip() else None

    def apply(self, document: dict) -> None:
        result = self.kubectl("apply", "-f", "-", content=json.dumps(document).encode())
        if result.returncode:
            raise RolloutAgentError("KUBERNETES_OPERATION_FAILED", "UNKNOWN")

    def release(self, name: str, namespace: str) -> dict | None:
        result = self.helm("status", name, "-n", namespace, "-o", "json")
        if result.returncode:
            if b"not found" in result.stderr:
                return None
            raise ClusterUnavailable()
        return json.loads(result.stdout).get("info", {})


def _marker(job: ProductionRolloutJob, chart: ArtifactRef, values: ArtifactRef) -> str:
    return f"cmp:{job.job_id}:{chart.sha256[:16]}:{values.sha256[:16]}"


class RolloutRunner:
    def __init__(self, target: ClusterTarget, fetch):
        """`fetch(ref) -> bytes` downloads one artifact of the current job."""
        self.target, self.fetch = target, fetch

    # --- read-only preflight -------------------------------------------------------------
    def preflight(self, job: ProductionRolloutJob) -> None:
        system = self.target.get("namespace", "kube-system")
        uid = (system or {}).get("metadata", {}).get("uid")
        if uid != self.target.cluster_uid or uid != job.cluster_uid:
            raise RolloutAgentError("CLUSTER_MISMATCH")
        if self.target.get("storageclass", job.storage_class) is None:
            raise RolloutAgentError("STORAGE_CLASS_MISSING")
        namespace = self.target.get("namespace", job.namespace)
        if namespace is not None:
            annotations = namespace["metadata"].get("annotations", {})
            if annotations.get(ENVIRONMENT) != job.environment_id:
                raise RolloutAgentError("NAMESPACE_NOT_OWNED")
            for ref in job.application.secret_refs:
                secret = self.target.get("secret", ref.name, job.namespace)
                if secret is None or ref.key not in secret.get("data", {}):
                    raise RolloutAgentError("SECRET_REFERENCE_MISSING")
        elif job.application.secret_refs:
            raise RolloutAgentError("SECRET_REFERENCE_MISSING")
        db_secret = self.target.get("secret", job.database.password_secret.name, job.namespace)
        if db_secret is not None and job.database.password_secret.key not in db_secret.get(
            "data", {}
        ):
            raise RolloutAgentError("SECRET_REFERENCE_MISSING")

    def _download(self, job: ProductionRolloutJob, workdir: Path) -> dict[str, Path]:
        paths = {}
        for ref in job.artifact_refs():
            content = self.fetch(ref)
            try:
                verify_artifact_bytes(ref, content)
            except ValueError:
                raise RolloutAgentError("ARTIFACT_DIGEST_MISMATCH") from None
            path = workdir / (ref.sha256 + (".tgz" if ref.kind == "CHART" else ".json"))
            path.write_bytes(content)
            paths[ref.sha256] = path
        return paths

    # --- mutation --------------------------------------------------------------------------
    def _prepare_namespace(self, job: ProductionRolloutJob) -> None:
        if self.target.get("namespace", job.namespace) is None:
            self.target.apply(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": job.namespace,
                        "labels": {MANAGED: "true"},
                        "annotations": {ENVIRONMENT: job.environment_id},
                    },
                }
            )
        ref = job.database.password_secret
        if self.target.get("secret", ref.name, job.namespace) is None:
            # Generated in the user's cluster; the value never leaves it.
            password = base64.b64encode(secrets.token_urlsafe(32).encode()).decode()
            self.target.apply(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "type": "Opaque",
                    "metadata": {
                        "name": ref.name,
                        "namespace": job.namespace,
                        "labels": {MANAGED: "true"},
                    },
                    "data": {ref.key: password},
                }
            )

    def _install(self, job, release, chart, values, paths) -> None:
        marker = _marker(job, chart, values)
        info = self.target.release(release, job.namespace)
        if info is not None:
            if info.get("status") != "deployed":
                # pending-*/failed: an earlier attempt may still be running or half-applied.
                raise RolloutAgentError("INSPECTION_REQUIRED", "UNKNOWN")
            if info.get("description") == marker:
                return  # Already applied with these exact approved bytes: verify only.
        result = self.target.helm(
            "upgrade",
            "--install",
            release,
            str(paths[chart.sha256]),
            "-n",
            job.namespace,
            "-f",
            str(paths[values.sha256]),
            "--description",
            marker,
            "--wait",
            "--timeout",
            self.target.helm_timeout,
            timeout=900,
        )
        if result.returncode:
            raise RolloutAgentError("HELM_RELEASE_FAILED", "UNKNOWN")

    # --- READY checks (E8) ---------------------------------------------------------------
    def _workload_ready(self, kind: str, name: str, namespace: str) -> None:
        resource = self.target.get(kind, name, namespace)
        if resource is None:
            raise RolloutAgentError("WORKLOAD_MISSING")
        status = resource.get("status", {})
        desired = resource["spec"].get("replicas", 1)
        if (
            desired < 1
            or status.get("observedGeneration", 0) < resource["metadata"]["generation"]
            or status.get("readyReplicas", 0) != desired
            or status.get("updatedReplicas", 0) != desired
            # A stuck rolling update still has a Ready old Pod; require no surplus/unavailable.
            or status.get("replicas", desired) != desired
            or status.get("unavailableReplicas", 0) != 0
        ):
            raise RolloutAgentError("WORKLOAD_NOT_READY")

    def _endpoints_ready(self, name: str, namespace: str) -> None:
        endpoints = self.target.get("endpoints", name, namespace) or {}
        if not any(subset.get("addresses") for subset in endpoints.get("subsets") or []):
            raise RolloutAgentError("SERVICE_ENDPOINT_NOT_READY")

    def verify(self, job: ProductionRolloutJob) -> None:
        ns, db, app = job.namespace, job.database, job.application
        pvc = self.target.get("pvc", db.release, ns)
        if (
            pvc is None
            or pvc.get("status", {}).get("phase") != "Bound"
            or pvc["spec"].get("storageClassName") != job.storage_class
        ):
            raise RolloutAgentError("PVC_NOT_BOUND")
        self._workload_ready("statefulset", db.release, ns)
        self._workload_ready("deployment", app.release, ns)
        self._endpoints_ready(db.release, ns)
        self._endpoints_ready(app.release, ns)
        probe = self.target.kubectl(
            "exec",
            "-n",
            ns,
            f"statefulset/{db.release}",
            "-c",
            "postgres",
            "--",
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-p",
            "5432",
        )
        if probe.returncode:
            raise RolloutAgentError("DATABASE_NOT_READY")
        health = self.target.kubectl(
            "get",
            "--raw",
            f"/api/v1/namespaces/{ns}/services/{app.release}:{app.port}/proxy{app.health_path}",
        )
        if health.returncode:  # kubectl returns non-zero for any non-2xx proxied response
            raise RolloutAgentError("APPLICATION_HEALTH_FAILED")
        for release, chart, values in [
            (db.release, db.chart, db.values),
            (app.release, app.chart, app.values),
        ]:
            info = self.target.release(release, ns) or {}
            if info.get("status") != "deployed" or info.get("description") != _marker(
                job, chart, values
            ):
                raise RolloutAgentError("RELEASE_NOT_DEPLOYED")

    def rollout(self, job: ProductionRolloutJob) -> tuple[str, str]:
        """Returns (SUCCEEDED, READY) or a FAILED/UNKNOWN code. Raises ClusterUnavailable
        before any mutation so the caller can keep the job RUNNING and retry later."""
        if job.cluster_uid != self.target.cluster_uid:
            return "FAILED", "CLUSTER_MISMATCH"
        try:
            self.preflight(job)
        except ClusterUnavailable:
            raise
        except RolloutAgentError as exc:
            return exc.status, exc.code
        try:
            with tempfile.TemporaryDirectory(prefix="cmp-rollout-") as directory:
                paths = self._download(job, Path(directory))
                self._prepare_namespace(job)
                db, app = job.database, job.application
                self._install(job, db.release, db.chart, db.values, paths)
                self._install(job, app.release, app.chart, app.values, paths)
        except AgentTransportError:
            return "UNKNOWN", "ARTIFACT_UNAVAILABLE"
        except RolloutAgentError as exc:
            if exc.code != "HELM_RELEASE_FAILED":
                return exc.status, exc.code
            # Helm did not reach Ready: report the first failing READY check instead.
        try:
            self.verify(job)
        except ClusterUnavailable:
            return "UNKNOWN", "INSPECTION_REQUIRED"
        except RolloutAgentError as exc:
            return exc.status, exc.code
        return "SUCCEEDED", "READY"


class RolloutAgent:
    """One outbound poll → rollout → report cycle. Supervision/looping belongs to the caller."""

    def __init__(
        self, agent_id: str, credential: str, client: AgentControlClient, target: ClusterTarget
    ):
        self.agent_id, self.credential, self.client, self.target = (
            agent_id,
            credential,
            client,
            target,
        )

    def poll_once(self) -> dict | None:
        claimed = self.client.poll_once(agent_id=self.agent_id, credential=self.credential)
        if claimed is None:
            return None
        job_id = claimed["job_id"]
        try:
            job = ProductionRolloutJob.model_validate(claimed["payload"])
        except ValidationError:
            return self._report(job_id, "FAILED", "UNSUPPORTED_JOB")
        if job.agent_id != self.agent_id or job.job_id != job_id:
            return self._report(job_id, "FAILED", "JOB_TARGET_MISMATCH")

        def fetch(ref: ArtifactRef) -> bytes:
            return self.client.download_artifact(
                agent_id=self.agent_id, credential=self.credential, job_id=job_id, sha256=ref.sha256
            )

        try:
            status, code = RolloutRunner(self.target, fetch).rollout(job)
        except ClusterUnavailable:
            # Nothing was changed; keep the job RUNNING and re-inspect on the next poll.
            return {"job_id": job_id, "status": "RUNNING", "code": "KUBERNETES_UNAVAILABLE"}
        return self._report(job_id, status, code)

    def _report(self, job_id: str, status: str, code: str) -> dict:
        self.client.report(
            agent_id=self.agent_id,
            credential=self.credential,
            job_id=job_id,
            status=status,
            code=code,
        )
        return {"job_id": job_id, "status": status, "code": code}


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll and run one approved ON_PREM rollout job")
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--cluster-uid", required=True)
    parser.add_argument("--helm-timeout", default="5m")
    args = parser.parse_args()
    agent = RolloutAgent(
        args.agent_id,
        _secret_file(args.credential_file),
        AgentControlClient(args.platform_url),
        ClusterTarget(args.kubeconfig, args.context, args.cluster_uid, args.helm_timeout),
    )
    print(json.dumps(agent.poll_once() or {"status": "IDLE"}))


if __name__ == "__main__":
    main()
