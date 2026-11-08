"""Sandbox network rules: default-deny NetworkPolicies, DNS, intent edges, allow-list egress proxy.

The Sandbox worker owns these (plan 2.7). The intent conversion itself (default-deny, DNS, intent
edges, app entry) is shared with Production in `cmp_providers.network_policy` (ticket 26); the
proxy rules below are Sandbox-only. Intent edges between resources are the only pod-to-pod
traffic allowed. Everything leaving the cluster must go through the egress proxy, whose allow list is the intent's `external_host`
entries plus the services kept at the source (`KEEP`). Every proxy decision is logged and read
back with `egress_records`.
"""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Literal

from cmp_providers import network_policy
from cmp_providers.network_policy import INSTANCE as _INSTANCE
from cmp_providers.network_policy import PRIVATE as _PRIVATE
from cmp_providers.network_policy import policy as _policy
from cmp_providers.network_policy import selector as _selector
from pydantic import BaseModel, ConfigDict

from cmp_worker.sandbox_redteam import REDTEAM_ROLE_LABEL, egress_policy

PROXY_NAME = "cmp-egress-proxy"
PROXY_PORT = 3128
# python:3.12.7-slim index digest (resolved from Docker Hub on 2026-11-05).
PROXY_IMAGE = "docker.io/library/python@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d"
ALLOW_ANNOTATION = "cmp.sandbox/allow-sha256"


class EgressRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["ALLOW", "BLOCK"]
    host: str
    port: int
    count: int


def proxy_env(release_names: list[str]) -> dict[str, str]:
    """Environment that sends an application's outbound HTTP(S) through the proxy."""
    url = f"http://{PROXY_NAME}:{PROXY_PORT}"
    no_proxy = ",".join(
        ["localhost", "127.0.0.1", ".svc", ".cluster.local", *sorted(release_names)]
    )
    values = {"HTTP_PROXY": url, "HTTPS_PROXY": url, "NO_PROXY": no_proxy}
    return {**values, **{k.lower(): v for k, v in values.items()}}


TELEMETRY_PORT = 443
# An operator slip must not open the sandbox: a range wider than this is ignored.
_MIN_PREFIX = {4: 16, 6: 48}


def telemetry_cidrs(value: str | None) -> list[dict[str, Any]]:
    """The platform's own OTLP ingest (`CMP_TELEMETRY_EGRESS_CIDR`, the value production allows).

    An app that exports telemetry (`OTEL_EXPORTER_OTLP_ENDPOINT`) calls this address on TCP 443 once
    it has traffic; without a rule the proxy records a BLOCK and the verdict fails with
    `EGRESS_BLOCKED`. Unset, empty, unparsable or too wide values give no rule.
    """
    try:
        net = ipaddress.ip_network((value or "").strip(), strict=False)
    except ValueError:
        return []
    if net.prefixlen < _MIN_PREFIX[net.version]:
        return []
    return [{"cidr": str(net), "port": TELEMETRY_PORT}]


def telemetry_cidr_from_env() -> str | None:
    return os.environ.get("CMP_TELEMETRY_EGRESS_CIDR", "").strip() or None


def allowlist(
    intents: list[dict[str, Any]], keep_hosts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    entries = {
        (i["external_host"].lower(), int(i["port"])) for i in intents if i.get("external_host")
    } | {(h["host"].lower(), int(h["port"])) for h in keep_hosts}
    return [{"host": host, "port": port} for host, port in sorted(entries)]


def build_manifests(
    *,
    namespace: str,
    labels: dict[str, str],
    resources: list[dict[str, Any]],
    releases: dict[str, str],
    intents: list[dict[str, Any]],
    allow: list[dict[str, Any]],
    proxy_script: str,
    redteam: bool = False,
    telemetry_cidr: str | None = None,
) -> list[dict[str, Any]]:
    apps = [r for r in resources if r["kind"] == "APPLICATION"]
    # Shared with Production (ticket 26): default-deny, DNS and resource → resource edges. With
    # `redteam`, the executor Pod (ticket 33) is left out of the DNS rule.
    manifests = network_policy.base_policies(
        namespace, labels, releases, intents,
        dns_exclude_label=REDTEAM_ROLE_LABEL if redteam else None,
    )  # fmt: skip
    # Test runners and ingress reach only the application port.
    manifests += [
        network_policy.entry(namespace, labels, a["release_name"], a["port"]) for a in apps
    ]

    if redteam:
        # Ticket 33: the executor reaches the first application's port and nothing else.
        manifests.append(egress_policy(namespace, labels, apps[0]["release_name"], apps[0]["port"]))

    # Applications may only leave through the proxy; the proxy keeps the allow list.
    app_selector = {"matchExpressions": [{"key": _INSTANCE, "operator": "In",
                                          "values": sorted(a["release_name"] for a in apps)}]}  # fmt: skip
    proxy_pods = _selector(PROXY_NAME)
    proxy_port = [{"protocol": "TCP", "port": PROXY_PORT}]
    manifests += [
        _policy(namespace, "cmp-apps-to-proxy", labels, {
            "podSelector": app_selector, "policyTypes": ["Egress"],
            "egress": [{"to": [{"podSelector": proxy_pods}], "ports": proxy_port}],
        }),
        _policy(namespace, "cmp-proxy-ingress", labels, {
            "podSelector": proxy_pods, "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"podSelector": app_selector}], "ports": proxy_port}],
        }),
        _policy(namespace, "cmp-proxy-egress", labels, {
            # Private ranges (pod, service, node networks) are excluded so the proxy cannot be
            # used to get around the pod-to-pod rules above.
            "podSelector": proxy_pods, "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": _PRIVATE}}]}],
        }),
    ]  # fmt: skip

    config: dict[str, Any] = {"allow": allow}
    if cidrs := telemetry_cidrs(telemetry_cidr):  # no key at all otherwise: the file is unchanged
        config["cidrs"] = cidrs
    allow_json = json.dumps(config, sort_keys=True)
    allow_hash = hashlib.sha256(allow_json.encode()).hexdigest()
    manifests += [
        {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": PROXY_NAME, "namespace": namespace, "labels": labels},
            "data": {"proxy.py": proxy_script, "allow.json": allow_json},
        },
        {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": PROXY_NAME, "namespace": namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {_INSTANCE: PROXY_NAME}},
                "template": {
                    "metadata": {
                        "labels": {_INSTANCE: PROXY_NAME},
                        "annotations": {ALLOW_ANNOTATION: allow_hash},
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [{
                            "name": "proxy", "image": PROXY_IMAGE,
                            "command": ["python", "-u", "/config/proxy.py", "/config/allow.json"],
                            "ports": [{"containerPort": PROXY_PORT}],
                            "readinessProbe": {"tcpSocket": {"port": PROXY_PORT}},
                            "volumeMounts": [{"name": "config", "mountPath": "/config"}],
                            "resources": {
                                "requests": {"cpu": "20m", "memory": "32Mi"},
                                "limits": {"cpu": "200m", "memory": "128Mi"},
                            },
                            "securityContext": {
                                "runAsNonRoot": True, "runAsUser": 65534,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }],
                        "volumes": [{"name": "config", "configMap": {"name": PROXY_NAME}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": PROXY_NAME, "namespace": namespace, "labels": labels},
            "spec": {
                "selector": {_INSTANCE: PROXY_NAME},
                "ports": [{"port": PROXY_PORT, "targetPort": PROXY_PORT}],
            },
        },
    ]  # fmt: skip
    return manifests


def proxy_script_text() -> str:
    return (Path(__file__).parent / "egress_proxy.py").read_text()


def parse_records(log: str) -> list[EgressRecord]:
    """Aggregate proxy log lines; anything that is not a decision line is ignored."""
    counts: dict[tuple[str, str, int], int] = {}
    for line in log.splitlines():
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
            key = (item["decision"], str(item["host"]), int(item["port"]))
        except (ValueError, KeyError, TypeError):
            continue
        if key[0] in {"ALLOW", "BLOCK"}:
            counts[key] = counts.get(key, 0) + 1
    return [
        EgressRecord(decision=d, host=h, port=p, count=c)  # type: ignore[arg-type]
        for (d, h, p), c in sorted(counts.items())
    ]
