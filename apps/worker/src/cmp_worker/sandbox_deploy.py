"""Sandbox configuration, apply and cleanup on the local `cmp-sandbox` k3d cluster (AV-011).

The Sandbox worker decides every concrete value (namespace, release names, StorageClass, the
generated database password, the app's `.env` Secret). Rendering stays in the shared renderer.
Secrets reach the cluster only through stdin; they are never placed on a command line, in
Values, in results or in logs. Only the dedicated `k3d-cmp-sandbox` context is accepted, and
only namespaces this module labelled can be deleted.
"""

import base64
import hashlib
import json
import re
import secrets
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cmp_contracts.provisioning import Image
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.shared_deployment_v2 import SharedDeploymentRequestV2
from cmp_providers.database_url import URL_SECRET_KEY, connection_url, connection_url_secret_name
from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2
from pydantic import BaseModel, ConfigDict, ValidationError

from cmp_worker.activities.k8s_workloads_cli import CommandError

SANDBOX_CONTEXT = "k3d-cmp-sandbox"
NAMESPACE_PREFIX = "cmp-sbx-"
MANAGED_BY = "cmp-sandbox"
FIELD_MANAGER = "cmp-sandbox"
LABEL_RUN = "cmp.sandbox/run-id"
LABEL_EXPIRES = "cmp.sandbox/expires-at"
ANNOTATION_CONTENT = "cmp.sandbox/content-sha256"
BASELINE_PATH = "/var/lib/postgresql/data/cmp-baseline.dump"
DEFAULT_TTL = timedelta(hours=2)
_NAMESPACE = re.compile(r"^cmp-sbx-[a-z0-9][a-z0-9-]*$")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class SandboxError(Exception):
    """Code only: command output and secret values are never part of the message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _slug(value: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:limit].strip("-") or "x"


def sandbox_namespace(migration_id: str, run_id: str) -> str:
    digest = hashlib.sha256(f"{migration_id}\0{run_id}".encode()).hexdigest()[:8]
    room = 63 - len(NAMESPACE_PREFIX) - 1 - len(digest)
    return f"{NAMESPACE_PREFIX}{_slug(f'{migration_id}-{run_id}', room)}-{digest}"


def release_name(prefix: str, resource_id: str, limit: int = 53) -> str:
    slug = _slug(resource_id, 200)
    room = limit - len(prefix) - 1
    if len(slug) > room:
        tail = hashlib.sha256(resource_id.encode()).hexdigest()[:6]
        slug = f"{slug[: room - 7].rstrip('-')}-{tail}"
    return f"{prefix}-{slug}"


class SandboxCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    storage_class: str
    provider: Literal["AWS", "GCP", "ON_PREM"] = "ON_PREM"


class SandboxImages(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    database: Image
    applications: dict[str, Image]


class SandboxPlan(BaseModel):
    """Everything needed to apply; holds secret *names* only, never values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: str
    run_id: str
    expires_at: int
    releases: dict[str, str]
    secrets: list[str]
    request: dict[str, Any]
    env_updated: list[str] = []  # app env Secrets whose content changed in this prepare


class ResourceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    image_digest: str
    values_sha256: str
    action: Literal["CREATED", "UNCHANGED", "UPGRADED", "RESTARTED", "RECREATED"]


class AttemptRecord(BaseModel):
    """What one attempt did to the shared Sandbox; digests and hashes only, never secrets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt: int
    resources: dict[str, ResourceRecord]
    baseline: Literal["KEPT", "LOST"]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def plan_update(previous: SandboxPlan, current: SandboxPlan) -> dict[str, str]:
    """Decide, per resource, the smallest change that brings the Sandbox to `current`."""
    old = {r["id"]: r for r in previous.request["resources"]}
    new = {r["id"]: r for r in current.request["resources"]}
    if set(old) != set(new):
        raise SandboxError("RESOURCE_SET_CHANGED")
    env_changed = {name.removesuffix("-env") for name in current.env_updated}
    actions: dict[str, str] = {}
    for resource_id, after in new.items():
        before = old[resource_id]
        if before == after:
            same_env = after["release_name"] in env_changed
            actions[resource_id] = "RESTARTED" if same_env else "UNCHANGED"
        elif after["kind"] == "POSTGRESQL":
            immutable = (
                before["version"] != after["version"]
                or before["storage"] != after["storage"]
                or before["database"] != after["database"]
                or before["release_name"] != after["release_name"]
            )
            actions[resource_id] = "RECREATED" if immutable else "UPGRADED"
        else:
            actions[resource_id] = "UPGRADED"
    return actions


class SandboxCluster:
    def __init__(self, runner: Any, *, kubeconfig: Path | str, context: str):
        if context != SANDBOX_CONTEXT:
            raise SandboxError("CONTEXT_NOT_ALLOWED")
        self._runner = runner
        self._kubeconfig = str(kubeconfig)
        self.context = context

    @property
    def helm_bin(self) -> str:
        return self._runner.helm

    def kubectl(self, *args: str, stdin: bytes | None = None, timeout: int = 300) -> bytes:
        command = [
            self._runner.kubectl, "--kubeconfig", self._kubeconfig, "--context", self.context, *args,
        ]  # fmt: skip
        return self._run(command, stdin, timeout)

    def helm(self, *args: str, timeout: int = 600) -> bytes:
        command = [
            self._runner.helm, "--kubeconfig", self._kubeconfig, "--kube-context", self.context, *args,
        ]  # fmt: skip
        return self._run(command, None, timeout)

    def _run(self, command: list[str], stdin: bytes | None, timeout: int) -> bytes:
        try:
            return self._runner.run(command, stdin=stdin, timeout=timeout)
        except CommandError:
            raise SandboxError("CLUSTER_COMMAND_FAILED") from None


def _env_values(path: Path | None, names: list[str]) -> dict[str, str]:
    """Read only the requested names from the `.env` file; values stay in memory."""
    found: dict[str, str] = {}
    if path is not None and path.is_file():
        for line in path.read_text(errors="ignore").splitlines():
            match = _ENV_LINE.match(line)
            if match and match.group(1) in names:
                value = match.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                found[match.group(1)] = value
    if set(names) - set(found):
        raise SandboxError("ENV_VALUE_MISSING")
    return found


class SandboxService:
    def __init__(
        self,
        cluster: SandboxCluster,
        *,
        database_chart_dir: Path,
        application_chart_dir: Path,
        password_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
    ) -> None:
        self.cluster = cluster
        self.database_chart_dir = Path(database_chart_dir)
        self.application_chart_dir = Path(application_chart_dir)
        self._password_factory = password_factory

    # ------------------------------------------------------------------ cluster helpers

    def _labels(self, run_id: str, expires_at: int) -> dict[str, str]:
        return {
            "app.kubernetes.io/managed-by": MANAGED_BY,
            LABEL_RUN: run_id,
            LABEL_EXPIRES: str(expires_at),
        }

    def _secret_exists(self, namespace: str, name: str) -> bool:
        out = self.cluster.kubectl(
            "get", "secret", name, "-n", namespace, "--ignore-not-found", "-o", "name"
        )
        return bool(out.strip())

    def _read_secret(self, namespace: str, name: str, key: str) -> str:
        out = self.cluster.kubectl(
            "get", "secret", name, "-n", namespace, "--ignore-not-found",
            "-o", f"jsonpath={{.data.{key}}}",
        )  # fmt: skip
        try:
            return base64.b64decode(out).decode()
        except (ValueError, UnicodeDecodeError):
            raise SandboxError("SECRET_UNREADABLE") from None

    def _secret_manifest(
        self,
        namespace: str,
        name: str,
        data: dict[str, str],
        labels: dict[str, str],
        annotations: dict[str, str] | None = None,
    ) -> bytes:
        metadata: dict[str, Any] = {"name": name, "namespace": namespace, "labels": labels}
        if annotations:
            metadata["annotations"] = annotations
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": metadata,
            "type": "Opaque",
            "stringData": data,
        }
        return json.dumps(manifest).encode()

    def _create_secret(
        self,
        namespace: str,
        name: str,
        data: dict[str, str],
        labels: dict[str, str],
        annotations: dict[str, str] | None = None,
    ) -> None:
        self.cluster.kubectl(
            "create", "-f", "-",
            stdin=self._secret_manifest(namespace, name, data, labels, annotations),
        )  # fmt: skip

    def _stored_content_hash(self, namespace: str, name: str) -> str:
        out = self.cluster.kubectl(
            "get", "secret", name, "-n", namespace, "--ignore-not-found",
            "-o", f'go-template={{{{index .metadata.annotations "{ANNOTATION_CONTENT}"}}}}',
        )  # fmt: skip
        return out.decode(errors="ignore").strip()

    # ------------------------------------------------------------------------ prepare

    def prepare_sandbox(
        self,
        requirements: RequirementsV2,
        *,
        migration_id: str,
        run_id: str,
        images: SandboxImages,
        capabilities: SandboxCapabilities,
        env_names: list[str],
        env_file: Path | None,
        now: datetime | None = None,
        ttl: timedelta = DEFAULT_TTL,
        update_env: bool = False,
    ) -> SandboxPlan:
        started = now or datetime.now(UTC)
        expires_at = int((started + ttl).timestamp())
        namespace = sandbox_namespace(migration_id, run_id)
        labels = self._labels(run_id, expires_at)

        databases = [r for r in requirements.resources if r.kind == "POSTGRESQL"]
        apps = [r for r in requirements.resources if r.kind == "APPLICATION"]
        if len(databases) != 1 or not apps:
            raise SandboxError("UNSUPPORTED_RESOURCE_GRAPH")
        db = databases[0]
        releases = {
            db.id: release_name("db", db.id),
            **{a.id: release_name("app", a.id) for a in apps},
        }
        db_release = releases[db.id]
        password_secret = f"{db_release}-auth"
        url_secret = connection_url_secret_name(db_release)
        secret_names = [password_secret]

        resources: list[dict[str, Any]] = [
            {
                "id": db.id, "kind": "POSTGRESQL", "release_name": db_release,
                "image": images.database.model_dump(mode="json"), "version": db.version,
                "extensions": [],
                "database": {
                    "name": db.database.name, "username": db.database.username,
                    "password_secret_ref": {"name": password_secret, "key": "password"},
                },
                "resources": {"cpu_millicores": db.cpu_millicores, "memory_mib": db.memory_mib},
                "storage": {"class_name": capabilities.storage_class, "size_gib": db.storage_gib},
            }
        ]  # fmt: skip
        needs_url = False
        app_env_values: dict[str, dict[str, str]] = {}
        for app in apps:
            bindings: dict[str, Any] = {}
            for name, binding in app.env_bindings.items():
                bindings[name] = binding.model_dump(mode="json")
                needs_url = needs_url or binding.output == "connection_url_secret_ref"
            wanted = [n for n in env_names if n not in bindings]
            env_secret = f"{releases[app.id]}-env"
            if wanted:
                app_env_values[env_secret] = _env_values(env_file, wanted)
                secret_names.append(env_secret)
                for name in wanted:
                    bindings[name] = {"secret_ref": {"name": env_secret, "key": name}}
            if app.id not in images.applications:
                raise SandboxError("APPLICATION_IMAGE_MISSING")
            resources.append(
                {
                    "id": app.id, "kind": "APPLICATION", "release_name": releases[app.id],
                    "image": images.applications[app.id].model_dump(mode="json"),
                    "replicas": app.replicas,
                    "resources": {"cpu_millicores": app.cpu_millicores, "memory_mib": app.memory_mib},
                    "port": app.port, "health_path": app.health_path, "env_bindings": bindings,
                }
            )  # fmt: skip
        if needs_url:
            secret_names.append(url_secret)
        request = {
            "contract_version": "2",
            "environment": "SANDBOX",
            "provider": capabilities.provider,
            "namespace": namespace,
            "resources": resources,
        }
        try:
            SharedDeploymentRequestV2.model_validate(request)
        except ValidationError:
            raise SandboxError("INVALID_SANDBOX_REQUEST") from None

        self.cluster.kubectl(
            "apply", "--server-side", f"--field-manager={FIELD_MANAGER}", "-f", "-",
            stdin=json.dumps(
                {
                    "apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": namespace, "labels": labels},
                }
            ).encode(),
        )  # fmt: skip

        password: str | None = None
        if not self._secret_exists(namespace, password_secret):
            password = self._password_factory()
            self._create_secret(namespace, password_secret, {"password": password}, labels)
        if needs_url and not self._secret_exists(namespace, url_secret):
            password = password or self._read_secret(namespace, password_secret, "password")
            url = connection_url(
                username=db.database.username,
                password=password,
                host=f"{db_release}.{namespace}.svc",
                port=5432,
                database=db.database.name,
            )
            self._create_secret(namespace, url_secret, {URL_SECRET_KEY: url}, labels)
        env_updated: list[str] = []
        for name, values in app_env_values.items():
            # The hash is salted by run and only lives on the Secret in the cluster.
            digest = hashlib.sha256(run_id.encode() + b"\0" + _canonical(values)).hexdigest()
            annotations = {ANNOTATION_CONTENT: digest}
            if not self._secret_exists(namespace, name):
                self._create_secret(namespace, name, values, labels, annotations)
            elif update_env and self._stored_content_hash(namespace, name) != digest:
                self.cluster.kubectl(
                    "replace", "-f", "-",
                    stdin=self._secret_manifest(namespace, name, values, labels, annotations),
                )  # fmt: skip
                env_updated.append(name)

        return SandboxPlan(
            namespace=namespace,
            run_id=run_id,
            expires_at=expires_at,
            releases=releases,
            secrets=secret_names,
            request=request,
            env_updated=env_updated,
        )

    # -------------------------------------------------------------------------- apply

    def apply_sandbox(self, plan: SandboxPlan) -> dict[str, Any]:
        rendered = self._render(plan)
        ordered = sorted(rendered.resources, key=lambda r: r.kind != "POSTGRESQL")
        endpoints: dict[str, str] = {}
        for resource in ordered:
            self._helm_release(plan, resource)
            port = 5432 if resource.kind == "POSTGRESQL" else self._app_port(plan, resource.id)
            endpoints[resource.id] = f"{resource.release_name}.{plan.namespace}.svc:{port}"
        return {
            "namespace": plan.namespace,
            "releases": plan.releases,
            "endpoints": endpoints,
            "resources": self._records(rendered),
        }

    def _helm_release(self, plan: SandboxPlan, resource: Any) -> None:
        chart = (
            self.database_chart_dir if resource.kind == "POSTGRESQL" else self.application_chart_dir
        )
        with tempfile.TemporaryDirectory(prefix="cmp-sbx-values-") as directory:
            path = Path(directory) / "values.json"
            path.write_text(json.dumps(resource.helm_values.model_dump(mode="json")))
            path.chmod(0o600)
            self.cluster.helm(
                "upgrade", resource.release_name, str(chart), "--install", "--wait",
                "--timeout", "5m", "--namespace", plan.namespace, "--values", str(path),
                timeout=420,
            )  # fmt: skip

    def _render(self, plan: SandboxPlan) -> Any:
        return render_shared_deployment_v2(
            plan.request,
            database_chart_dir=self.database_chart_dir,
            application_chart_dir=self.application_chart_dir,
            helm_bin=self.cluster.helm_bin,
        )

    # ------------------------------------------------------------------------- update

    def update_sandbox(
        self, previous: SandboxPlan, plan: SandboxPlan, attempt: int
    ) -> AttemptRecord:
        """Bring the running Sandbox to `plan` with the smallest change per resource."""
        actions = plan_update(previous, plan)
        rendered = self._render(plan)
        by_id = {r.id: r for r in rendered.resources}
        recreated = [i for i, a in actions.items() if a == "RECREATED"]
        for resource_id in recreated:
            release = by_id[resource_id].release_name
            self.cluster.helm(
                "uninstall", release, "--namespace", plan.namespace, "--wait", timeout=420
            )
            # A StatefulSet keeps its volume after uninstall; a new layout needs a fresh one.
            self.cluster.kubectl(
                "delete", "pvc", release, "-n", plan.namespace, "--ignore-not-found",
                "--wait=true", timeout=300,
            )  # fmt: skip
        ordered = sorted(rendered.resources, key=lambda r: r.kind != "POSTGRESQL")
        for resource in ordered:
            if actions[resource.id] in {"UPGRADED", "RECREATED"}:
                self._helm_release(plan, resource)
        for resource in ordered:
            if resource.kind != "APPLICATION":
                continue
            if actions[resource.id] == "RESTARTED" or (
                recreated and actions[resource.id] == "UNCHANGED"
            ):
                self._restart(plan.namespace, resource.release_name)
                actions[resource.id] = "RESTARTED"
        digests = self._records(rendered)
        return AttemptRecord(
            attempt=attempt,
            resources={i: ResourceRecord(**digests[i], action=actions[i]) for i in digests},
            baseline="LOST" if recreated else "KEPT",
        )

    def _restart(self, namespace: str, release: str) -> None:
        self.cluster.kubectl(
            "rollout", "restart", f"deployment/{release}", "-n", namespace
        )  # fmt: skip
        self.cluster.kubectl(
            "rollout", "status", f"deployment/{release}", "-n", namespace, "--timeout=300s",
            timeout=330,
        )  # fmt: skip

    # --------------------------------------------------------------- baseline and reset

    @staticmethod
    def _database(plan: SandboxPlan) -> dict[str, Any]:
        return next(r for r in plan.request["resources"] if r["kind"] == "POSTGRESQL")

    def _in_database(self, plan: SandboxPlan, script: str) -> bytes:
        db = self._database(plan)
        # The pod reads its own Secret through its environment; no password crosses the CLI.
        return self.cluster.kubectl(
            "exec", "-n", plan.namespace, f"statefulset/{db['release_name']}", "--",
            "sh", "-c", script, timeout=600,
        )  # fmt: skip

    _PG = (
        'PGPASSWORD="$POSTGRES_PASSWORD" {tool} -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
    )

    def snapshot_baseline(self, plan: SandboxPlan) -> dict[str, str]:
        dump = self._PG.format(tool="pg_dump") + f" -Fc -f {BASELINE_PATH}"
        try:
            self._in_database(plan, dump)
        except SandboxError:
            raise SandboxError("BASELINE_SAVE_FAILED") from None
        return {"baseline": "SAVED"}

    def reset_test_data(self, plan: SandboxPlan) -> dict[str, str]:
        try:
            self._in_database(plan, f"test -s {BASELINE_PATH}")
        except SandboxError:
            raise SandboxError("BASELINE_MISSING") from None
        restore = (
            self._PG.format(tool="psql")
            + ' -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public" && '
            + self._PG.format(tool="pg_restore")
            + f" --no-owner --schema=public {BASELINE_PATH}"
        )
        try:
            self._in_database(plan, restore)
        except SandboxError:
            raise SandboxError("RESET_FAILED") from None
        for app in (r for r in plan.request["resources"] if r["kind"] == "APPLICATION"):
            self._restart(plan.namespace, app["release_name"])
        return {"reset": "OK"}

    @staticmethod
    def _records(rendered: Any) -> dict[str, dict[str, str]]:
        return {
            r.id: {
                "image_digest": r.helm_values.image.uri.rsplit("@", 1)[1],
                "values_sha256": hashlib.sha256(
                    _canonical(r.helm_values.model_dump(mode="json"))
                ).hexdigest(),
            }
            for r in rendered.resources
        }

    @staticmethod
    def _app_port(plan: SandboxPlan, resource_id: str) -> int:
        return next(r["port"] for r in plan.request["resources"] if r["id"] == resource_id)

    # -------------------------------------------------------------------------- check

    def check_sandbox(self, plan: SandboxPlan) -> dict[str, str]:
        db = next(r for r in plan.request["resources"] if r["kind"] == "POSTGRESQL")
        # The pod reads its own Secret through its environment; no password crosses the CLI.
        probe = 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select version()"'
        try:
            version = self.cluster.kubectl(
                "exec", "-n", plan.namespace, f"statefulset/{db['release_name']}", "--",
                "sh", "-c", probe, timeout=120,
            ).decode(errors="ignore")  # fmt: skip
        except SandboxError:
            raise SandboxError("DB_CHECK_FAILED") from None
        match = re.search(r"PostgreSQL (\d+)", version)
        if not match or match.group(1) != db["version"]:
            raise SandboxError("DB_VERSION_MISMATCH")
        for app in (r for r in plan.request["resources"] if r["kind"] == "APPLICATION"):
            path = (
                f"/api/v1/namespaces/{plan.namespace}/services/"
                f"{app['release_name']}:{app['port']}/proxy{app['health_path']}"
            )
            try:
                self.cluster.kubectl("get", "--raw", path, timeout=60)
            except SandboxError:
                raise SandboxError("HEALTH_CHECK_FAILED") from None
        return {"database_version": match.group(1), "sql": "OK", "health": "OK"}

    # ------------------------------------------------------------------------ cleanup

    def teardown_sandbox(self, namespace: str) -> None:
        if not _NAMESPACE.fullmatch(namespace):
            raise SandboxError("NOT_A_SANDBOX_NAMESPACE")
        self.cluster.kubectl(
            "delete", "namespace", namespace, "--ignore-not-found", "--wait=true",
            "--timeout=180s", timeout=240,
        )  # fmt: skip
        left = self.cluster.kubectl(
            "get", "namespace", namespace, "--ignore-not-found", "-o", "name"
        )
        if left.strip():
            raise SandboxError("TEARDOWN_INCOMPLETE")

    def cleanup_expired(self, *, now: datetime | None = None) -> list[str]:
        moment = int((now or datetime.now(UTC)).timestamp())
        listing = json.loads(
            self.cluster.kubectl(
                "get",
                "namespaces",
                "-l",
                f"app.kubernetes.io/managed-by={MANAGED_BY}",
                "-o",
                "json",
            )
            or b'{"items": []}'
        )
        deleted = []
        for item in listing.get("items", []):
            meta = item.get("metadata", {})
            expires = meta.get("labels", {}).get(LABEL_EXPIRES, "")
            if expires.isdigit() and int(expires) < moment and _NAMESPACE.fullmatch(meta["name"]):
                self.teardown_sandbox(meta["name"])
                deleted.append(meta["name"])
        return deleted
