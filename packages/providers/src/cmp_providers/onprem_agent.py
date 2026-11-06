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
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.network_preflight import PREFLIGHT_JOB_MODE, NetworkPreflightJob
from cmp_contracts.onprem_rollout import ProductionRolloutJob
from cmp_contracts.onprem_teardown import TEARDOWN_MODE, TeardownJob
from pydantic import ValidationError

from cmp_providers import agent_image_pull, agent_rollout_recovery
from cmp_providers.agent_teardown import TeardownRunner
from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.database_url import connection_url
from cmp_providers.db_replication import DB_PROBE, judge_probe
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


class ReleaseNotSettled(RolloutAgentError):
    """Ticket 51: the upgrade did not settle; `previous` is the deployed revision to restore
    (None when nothing ran before, e.g. a failed first install)."""

    def __init__(self, kind: str, release: str, previous: int | None):
        super().__init__("HELM_RELEASE_FAILED", "UNKNOWN")
        self.kind, self.release, self.previous = kind, release, previous


SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
IN_CLUSTER_CONTEXT = "cmp-in-cluster"


class ClusterTarget:
    """Every kubectl/helm call uses a dedicated kubeconfig and explicit context: the operator's
    file (host CLI) or one generated from the Pod's ServiceAccount (in-cluster, ticket 15 H1)."""

    def __init__(self, kubeconfig: Path, context: str, cluster_uid: str, helm_timeout: str = "5m"):
        if not Path(kubeconfig).is_file() or not context:
            raise RolloutAgentError("EXPLICIT_TARGET_REQUIRED")
        self.kubeconfig, self.context = Path(kubeconfig), context
        self.cluster_uid, self.helm_timeout = cluster_uid, helm_timeout

    @classmethod
    def in_cluster(
        cls,
        *,
        cluster_uid: str | None = None,
        helm_timeout: str = "5m",
        service_account_dir: Path = SERVICE_ACCOUNT_DIR,
        work_dir: Path | None = None,
    ) -> "ClusterTarget":
        """kubectl's implicit in-cluster fallback is not reliable across versions, so write an
        explicit kubeconfig that references the token *file* (rotation-safe, value not copied)."""
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT")
        token, ca = Path(service_account_dir) / "token", Path(service_account_dir) / "ca.crt"
        if not host or not port or not token.is_file() or not ca.is_file():
            raise RolloutAgentError("SERVICE_ACCOUNT_REQUIRED")
        server = f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"
        directory = Path(work_dir or Path(tempfile.gettempdir()) / "cmp-agent-kube")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "config"
        cluster = {"server": server, "certificate-authority": str(ca)}
        config = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "in-cluster", "cluster": cluster}],
            "users": [{"name": "agent", "user": {"tokenFile": str(token)}}],
            "contexts": [
                {"name": IN_CLUSTER_CONTEXT, "context": {"cluster": "in-cluster", "user": "agent"}}
            ],
            "current-context": IN_CLUSTER_CONTEXT,
        }
        descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output)
        return cls(path, IN_CLUSTER_CONTEXT, cluster_uid, helm_timeout)

    def discover_cluster_uid(self) -> str:
        """kube-system Namespace UID identifies the cluster; it is what registration binds."""
        uid = ((self.get("namespace", "kube-system") or {}).get("metadata") or {}).get("uid")
        if not uid:
            raise ClusterUnavailable()
        self.cluster_uid = uid
        return uid

    def delete_secret(self, name: str, namespace: str) -> None:
        result = self.kubectl("delete", "secret", name, "-n", namespace, "--ignore-not-found")
        if result.returncode:
            raise ClusterUnavailable()

    def _exec(self, argv: list[str], content: bytes | None, timeout: int):
        try:
            return subprocess.run(
                argv, input=content, capture_output=True, check=False, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError):
            raise ClusterUnavailable() from None

    def kubectl(self, *args: str, content: bytes | None = None, timeout: int = 40):
        explicit = ["--kubeconfig", str(self.kubeconfig), "--context", self.context]
        return self._exec(["kubectl", *explicit, "--request-timeout=30s", *args], content, timeout)

    def helm(self, *args: str, timeout: int = 60):
        explicit = ["--kubeconfig", str(self.kubeconfig), "--kube-context", self.context]
        return self._exec(["helm", *explicit, *args], None, timeout)

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

    def list_pull_secrets(self) -> list[dict]:
        """Ticket 29: managed pull Secrets in every Namespace. Keeps only names, labels and the
        platform's own `cmp.dev/*` annotations; data and `last-applied-configuration` (which
        `kubectl apply` fills with the whole object) are dropped immediately."""
        selector = f"{MANAGED}=true,{agent_image_pull.PURPOSE}={agent_image_pull.PULL_PURPOSE}"
        result = self.kubectl("get", "secrets", "-A", "-l", selector, "-o", "json")
        if result.returncode:
            raise ClusterUnavailable()
        found = []
        for item in json.loads(result.stdout or b"{}").get("items", []):
            meta = item.get("metadata") or {}
            annotations = meta.get("annotations") or {}
            found.append(
                {
                    "metadata": {
                        "name": meta.get("name"),
                        "namespace": meta.get("namespace"),
                        "labels": meta.get("labels") or {},
                        "annotations": {
                            k: v for k, v in annotations.items() if k.startswith("cmp.dev/")
                        },
                    }
                }
            )
        return found

    def release(self, name: str, namespace: str) -> dict | None:
        result = self.helm("status", name, "-n", namespace, "-o", "json")
        if result.returncode:
            if b"not found" in result.stderr:
                return None
            raise ClusterUnavailable()
        data = json.loads(result.stdout)
        return data.get("info", {}) | {"revision": data.get("version")}

    def history(self, name: str, namespace: str) -> list[dict]:
        """Ticket 51: revision numbers and statuses only (descriptions/manifests dropped)."""
        result = self.helm("history", name, "-n", namespace, "-o", "json", "--max", "50")
        if result.returncode:
            raise ClusterUnavailable()
        return [
            {"revision": item.get("revision"), "status": item.get("status")}
            for item in json.loads(result.stdout or b"[]")
        ]

    def now(self) -> float:
        return time.monotonic()

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)


def _marker(job: ProductionRolloutJob, chart: ArtifactRef, values: ArtifactRef) -> str:
    return f"cmp:{job.job_id}:{chart.sha256[:16]}:{values.sha256[:16]}"


class RolloutRunner:
    def __init__(self, target: ClusterTarget, fetch, fetch_env_secrets=None, fetch_image_pull=None):
        """`fetch(ref) -> bytes` downloads one artifact of the current job.
        `fetch_env_secrets() -> [{name, data}]` (ENV-04) returns the job's rendered EnvPlan
        Secrets; values stay in memory and are applied only inside this cluster.
        `fetch_image_pull() -> {registry, username, password, expires_at}` (ticket 29)."""
        self.target, self.fetch = target, fetch
        self.fetch_env_secrets = fetch_env_secrets
        self.approved_policies: set[str] = set()  # ticket 26: names applied by this rollout
        self.fetch_image_pull = fetch_image_pull
        # Ticket 27: {capable, code} after READY; None = not verified (probe did not run).
        self.replication: dict | None = None
        # Ticket 52 (D4): `target.now()` value after which no change may start; None = no limit.
        self.deadline: float | None = None
        # Ticket 52 (D17, C-14): the job that owned the pull Secret before this rollout.
        self._pull_owner: str | None = None

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
        url = job.database.connection_url
        managed = [url.secret.name] if url is not None else []
        managed += [secret.name for secret in job.application.env_secrets]  # ENV-04
        managed += [job.image_pull.secret] if job.image_pull else []  # ticket 29
        for name in managed:
            existing = self.target.get("secret", name, job.namespace)
            labels = ((existing or {}).get("metadata") or {}).get("labels") or {}
            if existing is not None and labels.get(MANAGED) != "true":
                raise RolloutAgentError("SECRET_NAME_CONFLICT")  # never overwrite a user Secret

    def _env_secrets(self, job: ProductionRolloutJob) -> list[dict]:
        """ENV-04: fetch before any mutation; the names and keys must be the job's."""
        if not job.application.env_secrets:
            return []
        if self.fetch_env_secrets is None:
            raise RolloutAgentError("ENV_SECRETS_NOT_CONFIGURED")
        try:
            secrets = self.fetch_env_secrets()
        except AgentTransportError:
            raise RolloutAgentError("ENV_SECRETS_UNAVAILABLE", "UNKNOWN") from None
        by_name = {secret["name"]: secret["data"] for secret in secrets}
        if set(by_name) != {s.name for s in job.application.env_secrets} or any(
            not set(s.keys) <= set(by_name[s.name]) for s in job.application.env_secrets
        ):
            raise RolloutAgentError("ENV_SECRETS_INVALID")
        return secrets

    def _image_pull(self, job: ProductionRolloutJob) -> dict | None:
        """Ticket 29 (I5): fetch the job's ECR pull credential before any mutation."""
        if job.image_pull is None:
            return None
        if self.fetch_image_pull is None:
            raise RolloutAgentError("IMAGE_PULL_NOT_CONFIGURED")
        try:
            return agent_image_pull.validate(self.fetch_image_pull(), job.image_pull.registry)
        except AgentTransportError:
            raise RolloutAgentError("IMAGE_PULL_CREDENTIALS_UNAVAILABLE", "UNKNOWN") from None
        except agent_image_pull.ImagePullError as exc:
            raise RolloutAgentError(exc.code) from None

    def _apply_image_pull(self, job: ProductionRolloutJob, credential: dict | None) -> None:
        if credential is None:
            return
        name, now = job.image_pull.secret, time.time()
        existing = self.target.get("secret", name, job.namespace) or {}
        owner = ((existing.get("metadata") or {}).get("annotations") or {}).get(
            agent_image_pull.JOB_ID
        )
        self._pull_owner = owner if owner and owner != job.job_id else None
        agent_image_pull.apply_pull_secret(
            self.target, job.namespace, name, job.job_id, credential, now
        )
        agent_image_pull.link_service_account(self.target, job.namespace, name)

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
    def _prepare_namespace(self, job: ProductionRolloutJob, env_secrets=()) -> None:
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
        if job.database.connection_url is not None:
            self._write_connection_url(job)
        for secret in env_secrets:  # ENV-04: create or refresh; stdin only, never logged
            self.target.apply(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "type": "Opaque",
                    "metadata": {
                        "name": secret["name"],
                        "namespace": job.namespace,
                        "labels": {MANAGED: "true"},
                    },
                    "data": {
                        key: base64.b64encode(value.encode()).decode()
                        for key, value in secret["data"].items()
                    },
                }
            )

    # --- NetworkPolicies (ticket 26) -------------------------------------------------------
    def _network_policies(self, job: ProductionRolloutJob, paths: dict[str, Path]) -> list[dict]:
        """Approved policies, checked read-only before any mutation: every object is a managed
        NetworkPolicy in the job Namespace, and no unmanaged policy holds one of the names."""
        if job.network_policies is None:
            return []
        try:
            policies = json.loads(paths[job.network_policies.sha256].read_bytes())["policies"]
            for item in policies:
                meta = item["metadata"]
                if (
                    item["kind"] != "NetworkPolicy"
                    or meta["namespace"] != job.namespace
                    or meta["labels"].get(MANAGED) != "true"
                ):
                    raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RolloutAgentError("INVALID_NETWORK_POLICIES") from None
        for item in policies:
            existing = self.target.get("networkpolicy", item["metadata"]["name"], job.namespace)
            labels = ((existing or {}).get("metadata") or {}).get("labels") or {}
            if existing is not None and labels.get(MANAGED) != "true":
                raise RolloutAgentError("NETWORK_POLICY_CONFLICT")  # never overwrite the user's
        return policies

    def _managed_policy_names(self, namespace: str) -> set[str]:
        listed = self.target.kubectl(
            "get", "networkpolicy", "-n", namespace, "-l", f"{MANAGED}=true", "-o", "json"
        )
        if listed.returncode:
            raise RolloutAgentError("KUBERNETES_OPERATION_FAILED", "UNKNOWN")
        return {item["metadata"]["name"] for item in json.loads(listed.stdout).get("items", [])}

    def _apply_network_policies(self, job: ProductionRolloutJob, policies: list[dict]) -> None:
        """Apply before Helm, so Pods start under the approved rules; then remove managed
        policies a previous Revision created that this one no longer approves. Jobs from Plans
        published before ticket 26 carry no artifact and leave the Namespace untouched."""
        if job.network_policies is None:
            return
        for item in policies:
            self.target.apply(item)
        wanted = {item["metadata"]["name"] for item in policies}
        for name in sorted(self._managed_policy_names(job.namespace) - wanted):
            result = self.target.kubectl(
                "delete", "networkpolicy", name, "-n", job.namespace, "--ignore-not-found"
            )
            if result.returncode:
                raise RolloutAgentError("KUBERNETES_OPERATION_FAILED", "UNKNOWN")

    def _write_connection_url(self, job: ProductionRolloutJob) -> None:
        """Ticket 19 (U2–U5): rebuilt from the current password on every rollout so it never
        drifts; the URL and password stay inside the user's cluster."""
        url, ref = job.database.connection_url, job.database.password_secret
        encoded = (
            (self.target.get("secret", ref.name, job.namespace) or {}).get("data") or {}
        ).get(ref.key)
        if not encoded:
            raise RolloutAgentError("SECRET_REFERENCE_MISSING")
        value = connection_url(
            username=url.username,
            password=base64.b64decode(encoded).decode("utf-8"),
            host=f"{job.database.release}.{job.namespace}.svc",
            port=5432,
            database=url.database,
        )
        self.target.apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": url.secret.name,
                    "namespace": job.namespace,
                    "labels": {MANAGED: "true"},
                },
                "data": {url.secret.key: base64.b64encode(value.encode()).decode()},
            }
        )

    def _install(self, job, kind, release, chart, values, paths) -> None:
        """Ticket 51 boundary: Release inspection → upgrade (no `--wait`) → settle. A failure
        raises `ReleaseNotSettled` with the revision `rollout` restores."""
        marker, namespace = _marker(job, chart, values), job.namespace
        info = self.target.release(release, namespace)
        if info is not None and info.get("status") == "failed":
            # Helm finished and failed (an older Agent, or an API error): inspect the history,
            # return to the last deployed Revision, then re-apply. None: nothing ran before.
            revision = agent_rollout_recovery.last_deployed_revision(
                self.target, release, namespace
            )
            if revision is not None:
                if not agent_rollout_recovery.restore(
                    self.target, kind, release, namespace, revision
                ):
                    raise RolloutAgentError("ROLLBACK_FAILED", "UNKNOWN")
                info = self.target.release(release, namespace)
        if info is not None:
            if info.get("status") not in ("deployed", "failed"):
                # pending-*/uninstalling: an earlier Helm step may still be running (52).
                raise RolloutAgentError("INSPECTION_REQUIRED", "UNKNOWN")
            if info.get("status") == "deployed" and info.get("description") == marker:
                return  # Already applied with these exact approved bytes: verify only.
        previous = info.get("revision") if info and info.get("status") == "deployed" else None
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
            "--timeout",
            self.target.helm_timeout,
            timeout=900,
        )
        if result.returncode or not agent_rollout_recovery.settle(
            self.target, kind, release, namespace
        ):
            raise ReleaseNotSettled(kind, release, previous)

    # --- READY checks (E8) ---------------------------------------------------------------
    def _workload_ready(self, kind: str, name: str, namespace: str) -> None:
        resource = self.target.get(kind, name, namespace)
        if resource is None:
            raise RolloutAgentError("WORKLOAD_MISSING")
        # A stuck rolling update still has a Ready old Pod; require no surplus/unavailable.
        if not agent_rollout_recovery.workload_settled(resource):
            if agent_image_pull.image_pull_failed(self.target, namespace, name):
                raise RolloutAgentError("IMAGE_PULL_FAILED")  # ticket 29 (I7)
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
        if db.connection_url is not None:
            secret = self.target.get("secret", db.connection_url.secret.name, ns) or {}
            if db.connection_url.secret.key not in (secret.get("data") or {}):
                raise RolloutAgentError("SECRET_REFERENCE_MISSING")
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
        if job.network_policies is not None and not self.approved_policies <= (
            self._managed_policy_names(ns)
        ):
            raise RolloutAgentError("NETWORK_POLICY_MISSING")
        for release, chart, values in [
            (db.release, db.chart, db.values),
            (app.release, app.chart, app.values),
        ]:
            info = self.target.release(release, ns) or {}
            if info.get("status") != "deployed" or info.get("description") != _marker(
                job, chart, values
            ):
                raise RolloutAgentError("RELEASE_NOT_DEPLOYED")

    def check_replication(self, job: ProductionRolloutJob) -> dict | None:
        """Ticket 27: the same SQL check as AWS/GCP, run in the DB container after READY.
        Output stays in memory; only the judged flag and code leave the Agent."""
        try:
            result = self.target.kubectl(
                "exec", "-n", job.namespace, f"statefulset/{job.database.release}",
                "-c", "postgres", "--", "sh", "-c", DB_PROBE, "sh", "127.0.0.1",
                timeout=60,
            )  # fmt: skip
        except ClusterUnavailable:
            return None
        if result.returncode:
            return None
        _, capable, code = judge_probe(result.stdout.decode("utf-8", "replace"))
        return {"capable": capable, "code": code}

    def rollout(self, job: ProductionRolloutJob) -> tuple[str, str]:
        """Returns (SUCCEEDED, READY) or a FAILED/UNKNOWN code. Raises ClusterUnavailable
        before any mutation so the caller can keep the job RUNNING and retry later."""
        outcome = self._rollout(job)
        if outcome != ("SUCCEEDED", "READY") and self._pull_owner is not None:
            self._return_pull_secret(job)
        return outcome

    def _return_pull_secret(self, job: ProductionRolloutJob) -> None:
        """Ticket 52 (D17, C-14): a failed Revision leaves the earlier one running (51), so its
        job keeps refreshing the pull token. Best effort: a failure only delays the refresh."""
        try:
            self.target.kubectl(
                "annotate", "secret", job.image_pull.secret, "-n", job.namespace,
                f"{agent_image_pull.JOB_ID}={self._pull_owner}", "--overwrite",
            )  # fmt: skip
        except RolloutAgentError:
            pass

    def _expired(self) -> bool:
        return self.deadline is not None and self.target.now() >= self.deadline

    def _rollout(self, job: ProductionRolloutJob) -> tuple[str, str]:
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
                env_secrets = self._env_secrets(job)
                policies = self._network_policies(job, paths)
                self.approved_policies = {item["metadata"]["name"] for item in policies}
                pull = self._image_pull(job)
                if self._expired():  # ticket 52 (D4): checked right before the first change
                    raise RolloutAgentError("JOB_EXPIRED")
                self._prepare_namespace(job, env_secrets)
                del env_secrets
                self._apply_image_pull(job, pull)
                del pull
                self._apply_network_policies(job, policies)
                db, app = job.database, job.application
                self._install(job, "statefulset", db.release, db.chart, db.values, paths)
                self._install(job, "deployment", app.release, app.chart, app.values, paths)
        except AgentTransportError:
            return "UNKNOWN", "ARTIFACT_UNAVAILABLE"
        except ReleaseNotSettled as exc:
            unsettled = exc  # report the first failing READY check, then restore (ticket 51)
        except RolloutAgentError as exc:
            return exc.status, exc.code
        else:
            unsettled = None
        try:
            self.verify(job)
        except ClusterUnavailable:
            outcome = ("UNKNOWN", "INSPECTION_REQUIRED")
        except RolloutAgentError as exc:
            outcome = (exc.status, exc.code)
        else:
            self.replication = self.check_replication(job)
            return "SUCCEEDED", "READY"
        if unsettled is not None and not self._restore(job, unsettled):
            return "UNKNOWN", "ROLLBACK_FAILED"
        return outcome

    def _restore(self, job: ProductionRolloutJob, failed: ReleaseNotSettled) -> bool:
        """Ticket 51 (C-02): return only the Release that did not settle to the revision that
        was deployed before this job. Nothing to restore after a failed first install."""
        if failed.previous is None:
            return True
        try:
            return agent_rollout_recovery.restore(
                self.target, failed.kind, failed.release, job.namespace, failed.previous
            )
        except RolloutAgentError:
            return False


class RolloutAgent:
    """One outbound poll → rollout (or Network Preflight probe) → report cycle.
    Supervision/looping belongs to the caller."""

    def __init__(
        self,
        agent_id: str,
        credential: str,
        client: AgentControlClient,
        target: ClusterTarget,
        preflight=None,
    ):
        """`preflight`: `agent_preflight.PreflightJobRunner`; None reports
        PREFLIGHT_NOT_CONFIGURED for NETWORK_PREFLIGHT jobs (MNP-01 N3)."""
        self.agent_id, self.credential, self.client, self.target = (
            agent_id,
            credential,
            client,
            target,
        )
        self.preflight = preflight

    def _preflight(self, job_id: str, payload) -> dict:
        try:
            job = NetworkPreflightJob.model_validate(payload)
        except ValidationError:
            return self._report(job_id, "FAILED", "UNSUPPORTED_JOB")
        if job.agent_id != self.agent_id or job.job_id != job_id:
            return self._report(job_id, "FAILED", "JOB_TARGET_MISMATCH")
        if self.preflight is None:
            return self._report(job_id, "FAILED", "PREFLIGHT_NOT_CONFIGURED")
        status, code, observation = self.preflight.run(job)
        return self._report(job_id, status, code, observation)

    def _deadline(self, expires_in) -> float | None:
        """Ticket 52 (D4): the platform sends seconds left, so node clock skew does not matter."""
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in < 0:
            return None
        return self.target.now() + expires_in

    def _teardown(self, job_id: str, payload: dict, deadline: float | None) -> dict:
        try:
            job = TeardownJob.model_validate(payload)
        except ValidationError:
            return self._report(job_id, "FAILED", "UNSUPPORTED_JOB")
        if job.agent_id != self.agent_id or job.job_id != job_id:
            return self._report(job_id, "FAILED", "JOB_TARGET_MISMATCH")
        try:
            status, code = TeardownRunner(self.target).run(job, deadline=deadline)
        except ClusterUnavailable:
            # Teardown is idempotent: keep the job RUNNING and run it again on the next poll.
            return {"job_id": job_id, "status": "RUNNING", "code": "KUBERNETES_UNAVAILABLE"}
        return self._report(job_id, status, code)

    def poll_once(self) -> dict | None:
        claimed = self.client.poll_once(agent_id=self.agent_id, credential=self.credential)
        if claimed is None:
            return None
        job_id = claimed["job_id"]
        payload = claimed.get("payload")
        deadline = self._deadline(claimed.get("expires_in_seconds"))
        if isinstance(payload, dict) and payload.get("mode") == PREFLIGHT_JOB_MODE:
            return self._preflight(job_id, payload)
        if isinstance(payload, dict) and payload.get("mode") == TEARDOWN_MODE:
            return self._teardown(job_id, payload, deadline)
        try:
            # Ticket 52 (D11): any job type this Agent does not know ends here, unchanged.
            job = ProductionRolloutJob.model_validate(claimed["payload"])
        except ValidationError:
            return self._report(job_id, "FAILED", "UNSUPPORTED_JOB")
        if job.agent_id != self.agent_id or job.job_id != job_id:
            return self._report(job_id, "FAILED", "JOB_TARGET_MISMATCH")

        def fetch(ref: ArtifactRef) -> bytes:
            return self.client.download_artifact(
                agent_id=self.agent_id, credential=self.credential, job_id=job_id, sha256=ref.sha256
            )

        def fetch_env_secrets() -> list[dict]:
            return self.client.env_secrets(
                agent_id=self.agent_id, credential=self.credential, job_id=job_id
            )

        def fetch_image_pull() -> dict:
            return self.client.image_pull(
                agent_id=self.agent_id, credential=self.credential, job_id=job_id
            )

        runner = RolloutRunner(self.target, fetch, fetch_env_secrets, fetch_image_pull)
        runner.deadline = deadline
        try:
            status, code = runner.rollout(job)
        except ClusterUnavailable:
            # Nothing was changed; keep the job RUNNING and re-inspect on the next poll.
            return {"job_id": job_id, "status": "RUNNING", "code": "KUBERNETES_UNAVAILABLE"}
        return self._report(job_id, status, code, replication=runner.replication)

    def _report(
        self,
        job_id: str,
        status: str,
        code: str,
        observation: dict | None = None,
        replication: dict | None = None,
    ):
        extra = {"observation": observation} if observation is not None else {}
        if replication is not None:
            extra["replication"] = replication
        self.client.report(
            agent_id=self.agent_id,
            credential=self.credential,
            job_id=job_id,
            status=status,
            code=code,
            **extra,
        )
        # The returned outcome is logged by the daemon: codes only, never the observation.
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
