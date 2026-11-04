"""kubectl/Helm adapter shared by tickets 09 and 11. Raises code-only errors; never echoes output.

Secrets go to the cluster only through stdin (`kubectl create -f -`, or server-side apply,
which records field names but never values — never client-side apply, which would copy the
value into the last-applied annotation). kubeconfig lives in a 0700 temporary directory for the
duration of one `connect` and is deleted afterwards (provider runtimes own that).
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from cmp_providers.artifacts import canonical_json_bytes

FIELD_MANAGER = "cmp-provisioning"
DB_PROBE = (
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
    "-v ON_ERROR_STOP=1 -tAc \"select current_setting('server_version_num') || '|' || "
    '(select (rolreplication or rolsuper)::text from pg_roles where rolname = current_user)"'
)


class CommandError(RuntimeError):
    """Carries the tool name only; stdout/stderr may hold cluster data and are dropped."""


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
        except (OSError, subprocess.TimeoutExpired):
            raise CommandError(Path(args[0]).name) from None
        if done.returncode != 0:
            raise CommandError(Path(args[0]).name)
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

    def ingress_status(self, namespace: str, name: str) -> dict | None:
        """First `status.loadBalancer.ingress` entry (`hostname` on AWS, `ip` on GKE)."""
        item = self._get("ingress", name, namespace)
        if item is None:
            return None
        entries = item.get("status", {}).get("loadBalancer", {}).get("ingress") or []
        return entries[0] if entries else None
