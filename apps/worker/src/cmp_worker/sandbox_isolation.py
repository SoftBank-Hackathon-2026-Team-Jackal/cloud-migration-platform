"""The red-team isolation session (ticket 33, ADR-008): prove it, hand out an executor, clean up.

`RedTeamIsolation.session` is the only way to get an executor Pod. It refuses unless the
Namespace is this run's Sandbox, applies the executor's two policies, has a verification Pod with
the executor's labels try every address the executor must not reach, and only when all of them
fail (and the application answers) creates the executor. Whatever happens, the Pods and the
temporary policy are removed and the original DNS rule is put back. There is no attack logic here.
"""

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from cmp_providers import network_policy

from cmp_worker.sandbox_deploy import (
    FIELD_MANAGER,
    LABEL_RUN,
    MANAGED_BY,
    SandboxCluster,
    SandboxError,
    SandboxPlan,
)
from cmp_worker.sandbox_network import PROXY_IMAGE, PROXY_NAME, PROXY_PORT
from cmp_worker.sandbox_redteam import (
    EGRESS_POLICY,
    NOT_ENFORCED,
    REDTEAM_ROLE,
    REDTEAM_ROLE_LABEL,
    UNREACHABLE,
    egress_policy,
    executor_name,
    executor_pod,
    is_address,
    isolation_checks,
    isolation_verdict,
    parse_verdicts,
    restricted_violations,
    verifier_name,
    verifier_pod,
)
from cmp_worker.sandbox_redteam import MANAGED_BY as REDTEAM_MANAGED_BY

NOT_ALLOWED = "REDTEAM_TARGET_NOT_ALLOWED"
CONCURRENCY_LIMIT = "REDTEAM_CONCURRENCY_LIMIT"
_SANDBOX_NAMESPACE = re.compile(r"^cmp-sbx-[a-z0-9][a-z0-9-]*$")
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_DNS_PORT = 53
_API_PORT = 443

assert REDTEAM_MANAGED_BY == MANAGED_BY  # the pure module repeats the value; they must agree


@dataclass(frozen=True)
class Executor:
    """What a later step needs to run its tools: where the executor is and the one address it may
    reach. The target address is fixed here; nothing downstream chooses a destination."""

    name: str
    namespace: str
    target_ip: str
    port: int


class RedTeamIsolation:
    def __init__(
        self,
        cluster: SandboxCluster,
        *,
        pause: float = 1.0,
        timeout: float = 90.0,
        deadline_seconds: int = 900,
        max_concurrent: int = 1,
        image: str = PROXY_IMAGE,
    ) -> None:
        self.cluster = cluster
        self._pause = pause
        self._timeout = timeout
        self._deadline = deadline_seconds
        self._max_concurrent = max_concurrent
        self._image = image

    # ------------------------------------------------------------------------- the session

    @contextmanager
    def session(self, plan: SandboxPlan, *, target_resource_id: str) -> Iterator[Executor]:
        app = self._guard(plan, target_resource_id)
        self._limit(plan)
        owned = {_MANAGED_BY_LABEL: MANAGED_BY, LABEL_RUN: plan.run_id}
        names = (executor_name(plan.run_id), verifier_name(plan.run_id))
        try:
            self._remove_pods(plan.namespace, names, wait=True)  # a retry's leftovers
            policies = [
                network_policy.allow_dns(plan.namespace, owned, exclude_label=REDTEAM_ROLE_LABEL),
                egress_policy(plan.namespace, owned, app["release_name"], app["port"]),
            ]
            self._apply(policies, server_side=True)
            checks = isolation_checks(**self._addresses(plan, app))
            self._verify(plan, checks)
            ip = next(c["host"] for c in checks if c["name"] == "app")
            pod = executor_pod(
                plan.namespace, plan.run_id, image=self._image, deadline_seconds=self._deadline
            )
            if restricted_violations(pod):
                raise SandboxError(NOT_ENFORCED)
            self._apply([pod])
            yield Executor(name=names[0], namespace=plan.namespace, target_ip=ip, port=app["port"])
        finally:
            self._remove_pods(plan.namespace, names, wait=False)
            self._best_effort(
                "delete", "networkpolicy", EGRESS_POLICY, "-n", plan.namespace,
                "--ignore-not-found", "--wait=false",
            )  # fmt: skip
            restore = network_policy.allow_dns(plan.namespace, owned)
            self._best_effort_apply([restore])

    # ------------------------------------------------------------------------ preconditions

    def _guard(self, plan: SandboxPlan, target_resource_id: str) -> dict[str, Any]:
        """Only this run's own Sandbox Namespace, only an application inside it."""
        if not _SANDBOX_NAMESPACE.match(plan.namespace):
            raise SandboxError(NOT_ALLOWED)
        try:
            raw = self.cluster.kubectl("get", "namespace", plan.namespace, "-o", "json")
            labels = json.loads(raw or b"{}").get("metadata", {}).get("labels") or {}
        except (SandboxError, ValueError, AttributeError):
            raise SandboxError(NOT_ALLOWED) from None
        if labels.get(LABEL_RUN) != plan.run_id or labels.get(_MANAGED_BY_LABEL) != MANAGED_BY:
            raise SandboxError(NOT_ALLOWED)
        app = next((r for r in plan.request["resources"] if r["id"] == target_resource_id), None)
        if app is None or app["kind"] != "APPLICATION":
            raise SandboxError(NOT_ALLOWED)
        return app

    def _limit(self, plan: SandboxPlan) -> None:
        """At most `max_concurrent` executors in the whole cluster; our own leftovers don't count."""
        own = {
            f"{plan.namespace}/{executor_name(plan.run_id)}",
            f"{plan.namespace}/{verifier_name(plan.run_id)}",
        }
        out = self.cluster.kubectl(
            "get", "pods", "-A", "-l", f"{REDTEAM_ROLE_LABEL}={REDTEAM_ROLE}", "-o",
            'jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{"\\n"}{end}',
        )  # fmt: skip
        running = {
            line.strip() for line in out.decode(errors="ignore").splitlines() if line.strip()
        }
        if len(running - own) >= self._max_concurrent:
            raise SandboxError(CONCURRENCY_LIMIT)

    def _service_ip(self, namespace: str, name: str) -> str | None:
        try:
            out = self.cluster.kubectl(
                "get", "service", name, "-n", namespace, "-o", "jsonpath={.spec.clusterIP}"
            ).decode(errors="ignore")
        except SandboxError:
            return None
        return out.strip() if is_address(out) else None

    def _addresses(self, plan: SandboxPlan, app: dict[str, Any]) -> dict[str, Any]:
        """Cluster IPs of what the verification Pod tries. Anything that cannot be found makes the
        isolation unprovable, which is reported like a leak."""
        app_ip = self._service_ip(plan.namespace, app["release_name"])
        if app_ip is None:
            raise SandboxError(UNREACHABLE)
        database = next((r for r in plan.request["resources"] if r["kind"] == "POSTGRESQL"), None)
        needed: dict[str, tuple[str, str] | None] = {
            "database": (plan.namespace, database["release_name"]) if database else None,
            "proxy": (plan.namespace, PROXY_NAME),
            "kube_dns": ("kube-system", "kube-dns"),
            "kubernetes_api": ("default", "kubernetes"),
        }
        ips: dict[str, str | None] = {
            key: self._service_ip(*where) if where else None for key, where in needed.items()
        }
        if any(ips[key] is None for key, where in needed.items() if where is not None):
            raise SandboxError(NOT_ENFORCED)
        db_port = int(database.get("port", network_policy.DB_PORT)) if database else 0
        return {
            "app": (app_ip, app["port"]),
            "database": (ips["database"], db_port) if database and ips["database"] else None,
            "proxy": (ips["proxy"], PROXY_PORT),
            "kube_dns": (ips["kube_dns"], _DNS_PORT),
            "kubernetes_api": (ips["kubernetes_api"], _API_PORT),
            "database_name": database["release_name"] if database else None,
        }

    # ------------------------------------------------------------------------ verification

    def _verify(self, plan: SandboxPlan, checks: list[dict[str, Any]]) -> None:
        pod = verifier_pod(
            plan.namespace, plan.run_id, image=self._image, checks=checks, deadline_seconds=120
        )
        if restricted_violations(pod):
            raise SandboxError(NOT_ENFORCED)
        self._apply([pod])
        name = pod["metadata"]["name"]
        deadline = time.monotonic() + self._timeout
        while True:
            phase = (
                self.cluster.kubectl(
                    "get", "pod", name, "-n", plan.namespace, "-o", "jsonpath={.status.phase}"
                )
                .decode()
                .strip()
            )
            if phase == "Succeeded":
                break
            if phase == "Failed" or time.monotonic() >= deadline:
                raise SandboxError(NOT_ENFORCED)
            time.sleep(self._pause)
        log = self.cluster.kubectl("logs", name, "-n", plan.namespace).decode(errors="ignore")
        code = isolation_verdict(parse_verdicts(log), checks)
        if code is not None:
            raise SandboxError(code)

    # ---------------------------------------------------------------------------- cluster

    def _apply(self, items: list[dict[str, Any]], *, server_side: bool = False) -> None:
        flags = ["--server-side", f"--field-manager={FIELD_MANAGER}", "--force-conflicts"]
        self.cluster.kubectl(
            "apply", *(flags if server_side else []), "-f", "-",
            stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}).encode(),
        )  # fmt: skip

    def _remove_pods(self, namespace: str, names: tuple[str, ...], *, wait: bool) -> None:
        arguments = ["delete", "pod", *names, "-n", namespace, "--ignore-not-found"]
        if wait:
            self.cluster.kubectl(*arguments, "--wait=true", "--timeout=60s", timeout=90)
        else:
            self._best_effort(*arguments, "--wait=false")

    def _best_effort(self, *arguments: str) -> None:
        """Cleanup must not hide the error that ended the session; the Namespace is removed with
        the Sandbox and the Pods end at their own deadline anyway."""
        try:
            self.cluster.kubectl(*arguments)
        except SandboxError:
            pass

    def _best_effort_apply(self, items: list[dict[str, Any]]) -> None:
        try:
            self._apply(items, server_side=True)
        except SandboxError:
            pass
