"""kubectl/Helm adapter shared by tickets 09 and 11. Raises code-only errors; never echoes output.

Secrets go to the cluster only through stdin (`kubectl create -f -`, or server-side apply,
which records field names but never values — never client-side apply, which would copy the
value into the last-applied annotation). kubeconfig lives in a 0700 temporary directory for the
duration of one `connect` and is deleted afterwards (provider runtimes own that).
"""

import base64
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from cmp_providers.artifacts import canonical_json_bytes
from cmp_providers.db_replication import DB_PROBE

FIELD_MANAGER = "cmp-provisioning"


log = logging.getLogger(__name__)

# stderr는 클러스터 데이터(값)를 담을 수 있어 그대로 남기지 않는다. 알려진 실패 유형만 코드로 바꿔 남긴다.
# 원인이 "kubectl"로만 남아 서버에서 다시 재현해야 했다(2026-11-07 TARGET_SCHEMA_PARTIAL의 실제 원인은
# 스키마 복원 중 "relation ... already exists"였다).
_REASONS = (
    (re.compile(rb"already exists"), "OBJECT_EXISTS"),
    (re.compile(rb"does not exist|not found|NotFound", re.IGNORECASE), "NOT_FOUND"),
    (re.compile(rb"permission denied|Forbidden|must be owner", re.IGNORECASE), "PERMISSION_DENIED"),
    (
        re.compile(
            rb"Unauthorized|password authentication failed|You must be logged in", re.IGNORECASE
        ),
        "UNAUTHORIZED",
    ),
    (re.compile(rb"syntax error"), "SQL_SYNTAX"),
    (re.compile(rb"violates|duplicate key"), "CONSTRAINT_VIOLATION"),
    (re.compile(rb"timed? ?out|deadline exceeded|i/o timeout", re.IGNORECASE), "TIMEOUT"),
    (
        re.compile(
            rb"connection refused|could not connect|no route to host|connection reset",
            re.IGNORECASE,
        ),
        "CONNECT_FAILED",
    ),
    (
        re.compile(
            rb"container not found|unable to upgrade connection|pod .* not running", re.IGNORECASE
        ),
        "POD_NOT_READY",
    ),
)


# 로그에 남겨도 되는 하위 명령 이름(이 목록 밖의 인자는 경로·스크립트·값일 수 있어 남기지 않는다)
_SUBCOMMANDS = frozenset({
    "apply", "annotate", "create", "delete", "exec", "get", "install", "label", "logs", "patch",
    "replace", "rollout", "run", "scale", "status", "uninstall", "upgrade", "wait",
})  # fmt: skip


def failure_reason(stderr: bytes | None) -> str:
    """stderr를 값 없이 실패 유형 코드로 바꾼다. 모르면 OTHER."""
    for pattern, code in _REASONS:
        if stderr and pattern.search(stderr):
            return code
    return "OTHER"


class CommandError(RuntimeError):
    """Carries the tool name only; stdout/stderr may hold cluster data and are dropped.

    `reason` is a value-free failure code (`failure_reason`), never the output itself."""

    def __init__(self, tool: str, reason: str = "OTHER"):
        super().__init__(tool)
        self.reason = reason


class CommandRunner:
    """Runs CLI tools with a fixed environment; subclasses add provider credentials."""

    def __init__(self, *, kubectl: str = "kubectl", helm: str = "helm", env: dict | None = None):
        self.kubectl, self.helm = kubectl, helm
        self._env = os.environ.copy()
        self._env.update(env or {})

    def run(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        timeout: int = 300,
        env: dict | None = None,
    ) -> bytes:
        try:
            done = subprocess.run(
                args,
                input=stdin,
                capture_output=True,
                timeout=timeout,
                env={**self._env, **env} if env else self._env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("command failed: %s reason=TIMEOUT", Path(args[0]).name)
            raise CommandError(Path(args[0]).name, "TIMEOUT") from None
        except OSError:
            raise CommandError(Path(args[0]).name, "NOT_INSTALLED") from None
        if done.returncode != 0:
            reason = failure_reason(done.stderr)
            # 도구 이름, 하위 명령(인자 첫 개), 종료 코드, 유형 코드만 남긴다. 인자·출력에는 값이 있을 수 있다.
            sub = next((a for a in args[1:] if a in _SUBCOMMANDS), "")
            log.warning("command failed: %s %s exit=%s reason=%s",
                        Path(args[0]).name, sub, done.returncode, reason)  # fmt: skip
            raise CommandError(Path(args[0]).name, reason)
        return done.stdout

    def http_get_ok(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False


class KubectlSession:
    def __init__(self, runtime: CommandRunner, kubeconfig: Path, workdir: Path):
        self._rt, self._kubeconfig, self._workdir = runtime, str(kubeconfig), workdir

    def _kubectl(self, *args: str, stdin: bytes | None = None, timeout: int = 300) -> bytes:
        return self._rt.run(
            [self._rt.kubectl, "--kubeconfig", self._kubeconfig, *args],
            stdin=stdin,
            timeout=timeout,
        )

    def _helm(self, *args: str, timeout: int = 600) -> bytes:
        return self._rt.run(
            [self._rt.helm, "--kubeconfig", self._kubeconfig, *args], timeout=timeout
        )

    def _get(self, kind: str, name: str, namespace: str | None = None) -> dict | None:
        scope = ["-n", namespace] if namespace else []
        out = self._kubectl("get", kind, name, *scope, "--ignore-not-found", "-o", "json")
        return json.loads(out) if out.strip() else None

    # --- prerequisites ---

    def storage_class(self, name: str) -> dict | None:
        return self._get("storageclass", name)

    def ensure_namespace(self, name: str) -> None:
        if self._get("namespace", name) is None:
            self.apply(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": name,
                        "labels": {"app.kubernetes.io/managed-by": FIELD_MANAGER},
                    },
                }
            )

    def apply(self, manifest: dict) -> None:
        self._kubectl(
            "apply",
            "--server-side",
            f"--field-manager={FIELD_MANAGER}",
            "-f",
            "-",
            stdin=json.dumps(manifest).encode(),
        )

    def secret_keys(self, namespace: str, name: str) -> set[str] | None:
        out = self._kubectl(
            "get", "secret", name, "-n", namespace, "--ignore-not-found",
            "-o", "go-template={{range $k, $v := .data}}{{$k}}\n{{end}}",
        )  # fmt: skip
        if not out.strip():
            exists = self._kubectl(
                "get", "secret", name, "-n", namespace, "--ignore-not-found", "-o", "name"
            )
            return set() if exists.strip() else None
        return {line for line in out.decode().splitlines() if line}

    def create_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": FIELD_MANAGER},
            },
            "type": "Opaque",
            "stringData": data,
        }
        self._kubectl("create", "-f", "-", stdin=json.dumps(manifest).encode())

    def secret_managed(self, namespace: str, name: str) -> bool | None:
        item = self._get("secret", name, namespace)
        if item is None:
            return None
        labels = (item.get("metadata") or {}).get("labels") or {}
        return labels.get("app.kubernetes.io/managed-by") == FIELD_MANAGER

    def secret_value(self, namespace: str, name: str, key: str) -> str | None:
        """Decoded value in memory only (ticket 19 URL build); never logged or returned."""
        item = self._get("secret", name, namespace)
        encoded = ((item or {}).get("data") or {}).get(key)
        return base64.b64decode(encoded).decode("utf-8") if encoded else None

    def apply_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": FIELD_MANAGER},
            },
            "type": "Opaque",
            "stringData": data,
        }
        self._kubectl("apply", "-f", "-", stdin=json.dumps(manifest).encode())

    def configmap_keys(self, namespace: str, name: str) -> set[str] | None:
        item = self._get("configmap", name, namespace)
        return None if item is None else set(item.get("data", {}))

    # --- Helm ---

    def release(self, namespace: str, name: str) -> dict | None:
        listed = json.loads(
            self._helm(
                # Explicit status filters: Helm 3 hides pending without them, Helm 4 dropped -a.
                "list",
                "-n",
                namespace,
                "--deployed",
                "--failed",
                "--pending",
                "--filter",
                f"^{name}$",
                "-o",
                "json",
            )
        )
        match = [item for item in listed if item.get("name") == name]
        if not match:
            return None
        values = json.loads(self._helm("get", "values", name, "-n", namespace, "-o", "json"))
        return {"status": match[0]["status"], "chart": match[0]["chart"], "values": values or {}}

    def helm_upgrade_install(self, namespace: str, name: str, chart: Path, values: dict) -> None:
        values_file = self._workdir / f"{name}-values.json"
        values_file.write_bytes(canonical_json_bytes(values))
        values_file.chmod(0o600)
        self._helm(
            "upgrade", "--install", name, str(chart), "-n", namespace, "-f", str(values_file)
        )

    # --- readiness ---

    def pvc_phase_of(self, namespace: str, name: str) -> str | None:
        item = self._get("pvc", name, namespace)
        return None if item is None else item.get("status", {}).get("phase")

    def workload_ready(self, namespace: str, kind: str, name: str) -> bool:
        item = self._get(kind, name, namespace)
        if item is None:
            return False
        spec, status = item.get("spec", {}), item.get("status", {})
        wanted = spec.get("replicas", 1)
        if status.get("observedGeneration", 0) < item["metadata"].get("generation", 0):
            return False
        if kind == "deployment":
            return (
                status.get("updatedReplicas", 0) == wanted
                and status.get("availableReplicas", 0) == wanted
            )
        return status.get("readyReplicas", 0) == wanted and status.get(
            "currentRevision"
        ) == status.get("updateRevision")

    def waiting_reasons(self, namespace: str, instance: str) -> set[str]:
        pods = json.loads(
            self._kubectl(
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                f"app.kubernetes.io/instance={instance}",
                "-o",
                "json",
            )
        )
        reasons = set()
        for pod in pods.get("items", []):
            for status in pod.get("status", {}).get("containerStatuses", []):
                waiting = status.get("state", {}).get("waiting")
                if waiting and waiting.get("reason"):
                    reasons.add(waiting["reason"])
        return reasons

    def database_probe(self, namespace: str, pod: str, host: str) -> str:
        out = self._kubectl(
            "exec", "-n", namespace, pod, "-c", "postgres", "--",
            "sh", "-c", DB_PROBE, "sh", host,
            timeout=60,
        )  # fmt: skip
        return out.decode().strip()

    def service_health(self, namespace: str, service: str, path: str) -> bool:
        try:
            self._kubectl(
                "get", "--raw",
                f"/api/v1/namespaces/{namespace}/services/{service}:http/proxy{path}",
                timeout=30,
            )  # fmt: skip
        except CommandError:
            return False
        return True

    # --- NetworkPolicy (ticket 26) ---

    def get_object(self, kind: str, name: str, namespace: str | None = None) -> dict | None:
        return self._get(kind, name, namespace)

    def network_policies(self, namespace: str) -> dict[str, bool]:
        listed = json.loads(self._kubectl("get", "networkpolicy", "-n", namespace, "-o", "json"))
        found = {}
        for item in listed.get("items", []):
            labels = item["metadata"].get("labels") or {}
            found[item["metadata"]["name"]] = (
                labels.get("app.kubernetes.io/managed-by") == FIELD_MANAGER
            )
        return found

    def delete_network_policy(self, namespace: str, name: str) -> None:
        self._kubectl("delete", "networkpolicy", name, "-n", namespace, "--ignore-not-found")

    def ingress_status(self, namespace: str, name: str) -> dict | None:
        """First `status.loadBalancer.ingress` entry (`hostname` on AWS, `ip` on GKE)."""
        item = self._get("ingress", name, namespace)
        if item is None:
            return None
        entries = item.get("status", {}).get("loadBalancer", {}).get("ingress") or []
        return entries[0] if entries else None

    # --- teardown (ticket 53) ---

    def helm_uninstall(self, namespace: str, name: str) -> None:
        """Any Release state (`pending-*`, `failed`) is removed; an absent one is a no-op."""
        self._helm("uninstall", name, "-n", namespace, "--ignore-not-found")

    def load_balancer_objects(self) -> list[tuple[str, str, str]]:
        """Every Ingress and `type: LoadBalancer` Service: they hold cloud LBs outside State."""
        found = []
        ingresses = json.loads(self._kubectl("get", "ingress", "-A", "-o", "json"))
        for item in ingresses.get("items", []):
            found.append(("ingress", item["metadata"]["namespace"], item["metadata"]["name"]))
        services = json.loads(self._kubectl("get", "service", "-A", "-o", "json"))
        for item in services.get("items", []):
            if item.get("spec", {}).get("type") == "LoadBalancer":
                found.append(("service", item["metadata"]["namespace"], item["metadata"]["name"]))
        return found

    def delete_object(self, kind: str, namespace: str | None, name: str) -> None:
        scope = ["-n", namespace] if namespace else []
        self._kubectl("delete", kind, name, *scope, "--ignore-not-found", "--wait=false")

    def claims(self, namespace: str) -> list[dict]:
        listed = json.loads(self._kubectl("get", "pvc", "-n", namespace, "-o", "json"))
        return [
            {"name": item["metadata"]["name"], "volume": item.get("spec", {}).get("volumeName")}
            for item in listed.get("items", [])
        ]

    def persistent_volumes(self) -> list[dict]:
        listed = json.loads(self._kubectl("get", "pv", "-o", "json"))
        volumes = []
        for item in listed.get("items", []):
            spec = item.get("spec", {})
            volumes.append(
                {
                    "name": item["metadata"]["name"],
                    "reclaim": spec.get("persistentVolumeReclaimPolicy"),
                    "handle": (spec.get("csi") or {}).get("volumeHandle"),
                    "namespace": (spec.get("claimRef") or {}).get("namespace"),
                }
            )
        return volumes

    def set_reclaim_policy(self, volume: str, policy: str) -> None:
        patch = {"spec": {"persistentVolumeReclaimPolicy": policy}}
        self._kubectl("patch", "pv", volume, "--type=merge", "-p", json.dumps(patch))
