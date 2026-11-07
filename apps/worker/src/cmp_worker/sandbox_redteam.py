"""Red-team executor isolation (ticket 33, ADR-008): build and prove it, nothing more.

The executor is a short-lived Pod that can talk to the target application's Service port and to
nothing else: no DNS, no database, no proxy, no other Namespace, no internet, no cluster API. This
module holds the names and the pure manifest builders; it contains no attack logic. Choosing and
running tests is later work (tickets 34 and 35) and always goes through this isolation first.
"""

import hashlib
import ipaddress
import json
from collections.abc import Mapping
from typing import Any

from cmp_providers import network_policy

# The label that marks the executor. Every policy that must not apply to it (DNS) excludes it,
# and no other Pod carries it.
REDTEAM_ROLE_LABEL = "cmp.sandbox/role"
REDTEAM_ROLE = "redteam-executor"
EGRESS_POLICY = "cmp-redteam-egress"


def egress_policy(
    namespace: str, labels: dict[str, str], target_release: str, port: int
) -> dict[str, Any]:
    """The executor's only way out: the target application's Pods, on its one TCP port.

    The Namespace's default-deny already closes everything else, DNS is withheld from this Pod
    (see `network_policy.allow_dns`), and nothing selects it for ingress. The application's own
    entry rule is what lets this traffic in.
    """
    return network_policy.policy(namespace, EGRESS_POLICY, labels, {
        "podSelector": {"matchLabels": {REDTEAM_ROLE_LABEL: REDTEAM_ROLE}},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{"podSelector": network_policy.selector(target_release)}],
            "ports": [{"protocol": "TCP", "port": port}],
        }],
    })  # fmt: skip


# --- the executor and its verification Pod -----------------------------------------------------

# Same value as `sandbox_deploy.MANAGED_BY`; a test keeps them equal (this module must not import
# `sandbox_deploy`, which imports `sandbox_network`, which imports this module).
MANAGED_BY = "cmp-sandbox"
LABEL_RUN = "cmp.sandbox/run-id"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_RESTRICTED_SECCOMP = {"RuntimeDefault", "Localhost"}


def _digest(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()[:8]


def executor_name(run_id: str) -> str:
    """Fixed per run, so a retry replaces a leftover Pod instead of adding another."""
    return f"cmp-redteam-{_digest(run_id)}"


def verifier_name(run_id: str) -> str:
    return f"cmp-redteam-vf-{_digest(run_id)}"


def _pod(
    namespace: str, name: str, run_id: str, image: str, command: list[str], deadline: int
) -> dict[str, Any]:
    labels = {_MANAGED_BY_LABEL: MANAGED_BY, LABEL_RUN: run_id, REDTEAM_ROLE_LABEL: REDTEAM_ROLE}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            # Without this the kubelet injects `<SERVICE>_SERVICE_HOST` variables for every Service
            # in the Namespace: the names and addresses of what the executor must not know.
            "enableServiceLinks": False,
            "activeDeadlineSeconds": deadline,
            "terminationGracePeriodSeconds": 1,
            "containers": [{
                "name": "executor",
                "image": image,
                "command": command,
                "resources": {
                    "requests": {"cpu": "10m", "memory": "32Mi"},
                    "limits": {"cpu": "250m", "memory": "128Mi"},
                },
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 65534,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
            }],
        },
    }  # fmt: skip


def executor_pod(
    namespace: str, run_id: str, *, image: str, deadline_seconds: int
) -> dict[str, Any]:
    """The Pod later tickets run their fixed tools in. Here it only waits: no environment, no
    token, no volume, no capability, and it ends by itself at the deadline."""
    holder = f"import time; time.sleep({int(deadline_seconds)})"
    return _pod(
        namespace, executor_name(run_id), run_id, image, ["python", "-c", holder], deadline_seconds
    )


def restricted_violations(pod: Mapping[str, Any]) -> list[str]:
    """What keeps a Pod from meeting Pod Security `restricted` plus the executor's own rules.

    The Namespace itself is not switched to `enforce: restricted`: the application and database
    charts were not written for it and a restart (a data reset restarts the applications) could
    fail. The executor is checked here instead, before it is applied.
    """
    spec = pod.get("spec", {})
    found = [f"{flag}: true" for flag in ("hostNetwork", "hostPID", "hostIPC") if spec.get(flag)]
    if spec.get("automountServiceAccountToken") is not False:
        found.append("serviceAccountToken is mounted")
    if spec.get("enableServiceLinks") is not False:
        found.append("serviceLinks inject Service addresses")
    if spec.get("volumes"):
        found.append("volumes")
    pod_seccomp = (spec.get("securityContext") or {}).get("seccompProfile", {}).get("type")
    for container in [*spec.get("containers", []), *spec.get("initContainers", [])]:
        context = container.get("securityContext") or {}
        if container.get("env") or container.get("envFrom"):
            found.append("env")
        if any(p.get("hostPort") for p in container.get("ports", [])):
            found.append("hostPort")
        if context.get("privileged"):
            found.append("privileged")
        if context.get("runAsNonRoot") is not True:
            found.append("runAsNonRoot is not set")
        if context.get("allowPrivilegeEscalation") is not False:
            found.append("allowPrivilegeEscalation is not false")
        if context.get("readOnlyRootFilesystem") is not True:
            found.append("readOnlyRootFilesystem is not set")
        capabilities = context.get("capabilities") or {}
        if capabilities.get("add") or capabilities.get("drop") != ["ALL"]:
            found.append("capabilities are not dropped to none")
        seccomp = (context.get("seccompProfile") or {}).get("type") or pod_seccomp
        if seccomp not in _RESTRICTED_SECCOMP:
            found.append("seccomp profile is not set")
        if "@sha256:" not in container.get("image", ""):
            found.append("image is not pinned by digest")
        limits = (container.get("resources") or {}).get("limits") or {}
        if not limits.get("cpu") or not limits.get("memory"):
            found.append("cpu and memory limits are missing")
    return found


# --- proving the isolation ---------------------------------------------------------------------

# Addresses the verification Pod tries, by name. Only `app` may answer; every other one is
# something the executor must not reach. `dns` is a name lookup, not a connection.
ALLOWED_CHECK = "app"
FORBIDDEN_CHECKS = (
    "db", "proxy", "other_namespace", "kubernetes_api", "metadata", "internet", "dns",
)  # fmt: skip
METADATA_ADDRESS = "169.254.169.254"
PUBLIC_ADDRESS = "1.1.1.1"
NOT_ENFORCED = "REDTEAM_ISOLATION_NOT_ENFORCED"
UNREACHABLE = "REDTEAM_TARGET_UNREACHABLE"

# Runs inside the verification Pod. It opens (or tries to open) one TCP connection per entry and
# prints one JSON line per check: reachable or not. It sends no payload and reads nothing back.
VERIFY_SCRIPT = (
    "import json, socket, sys, threading\n"
    "checks = json.loads(sys.argv[1])\n"
    "results = {}\n"
    "def tcp(name, host, port):\n"
    "    s = socket.socket()\n"
    "    s.settimeout(3)\n"
    "    try:\n"
    "        s.connect((host, port))\n"
    "        results[name] = True\n"
    "    except OSError:\n"
    "        results[name] = False\n"
    "    finally:\n"
    "        s.close()\n"
    "def dns(name, host, port):\n"
    "    try:\n"
    "        socket.getaddrinfo(host, None)\n"
    "        results[name] = True\n"
    "    except OSError:\n"
    "        results[name] = False\n"
    "threads = []\n"
    "for c in checks:\n"
    "    target = dns if c['kind'] == 'dns' else tcp\n"
    "    t = threading.Thread(target=target, args=(c['name'], c['host'], c['port']), daemon=True)\n"
    "    t.start()\n"
    "    threads.append(t)\n"
    "for t in threads:\n"
    "    t.join(8)\n"
    "for c in checks:\n"
    "    print(json.dumps({'check': c['name'], 'reachable': results.get(c['name'], False)}),\n"
    "          flush=True)\n"
)


def isolation_checks(
    *,
    app: tuple[str, int],
    database: tuple[str, int] | None,
    proxy: tuple[str, int] | None,
    kube_dns: tuple[str, int],
    kubernetes_api: tuple[str, int],
    database_name: str | None,
) -> list[dict[str, Any]]:
    """The connections the verification Pod tries. Every host is an IP address (the Pod has no
    DNS) except the one `dns` check, whose point is that a name does not resolve."""
    checks: list[dict[str, Any]] = [
        {"name": ALLOWED_CHECK, "kind": "tcp", "host": app[0], "port": app[1]},
    ]
    if database is not None:
        checks.append({"name": "db", "kind": "tcp", "host": database[0], "port": database[1]})
    if proxy is not None:
        checks.append({"name": "proxy", "kind": "tcp", "host": proxy[0], "port": proxy[1]})
    checks += [
        {"name": "other_namespace", "kind": "tcp", "host": kube_dns[0], "port": kube_dns[1]},
        {"name": "kubernetes_api", "kind": "tcp", "host": kubernetes_api[0],
         "port": kubernetes_api[1]},
        {"name": "metadata", "kind": "tcp", "host": METADATA_ADDRESS, "port": 80},
        {"name": "internet", "kind": "tcp", "host": PUBLIC_ADDRESS, "port": 443},
    ]  # fmt: skip
    if database_name:
        checks.append({"name": "dns", "kind": "dns", "host": database_name, "port": 0})
    return checks


def parse_verdicts(log: str) -> dict[str, bool]:
    """One entry per well-formed line; anything else in the log is ignored."""
    found: dict[str, bool] = {}
    for line in log.splitlines():
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(item, dict)
            and isinstance(item.get("check"), str)
            and isinstance(item.get("reachable"), bool)
        ):
            found[item["check"]] = item["reachable"]
    return found


def isolation_verdict(verdicts: Mapping[str, bool], checks: list[dict[str, Any]]) -> str | None:
    """None when the isolation holds, otherwise the error code.

    Fail closed: a check without a readable answer counts as a leak, and a leak is reported before
    an unreachable application, because the leak is what makes the executor unsafe to start.
    """
    names = [c["name"] for c in checks]
    if any(name not in verdicts for name in names):
        return NOT_ENFORCED
    if any(verdicts[name] for name in names if name != ALLOWED_CHECK):
        return NOT_ENFORCED
    if not verdicts[ALLOWED_CHECK]:
        return UNREACHABLE
    return None


def verifier_pod(
    namespace: str, run_id: str, *, image: str, checks: list[dict[str, Any]], deadline_seconds: int
) -> dict[str, Any]:
    """Carries the executor's labels, so the same policies that will bind the executor decide
    what this Pod can reach. The checks hold addresses and ports only."""
    command = ["python", "-c", VERIFY_SCRIPT, json.dumps(checks, separators=(",", ":"))]
    return _pod(namespace, verifier_name(run_id), run_id, image, command, deadline_seconds)


def is_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True
