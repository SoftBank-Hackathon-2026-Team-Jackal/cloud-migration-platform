"""Sandbox network rules: default-deny NetworkPolicies, DNS, intent edges, allow-list egress proxy.

The Sandbox worker owns these (plan 2.7); Production policy conversion belongs to Provisioning.
Intent edges between resources are the only pod-to-pod traffic allowed. Everything leaving the
cluster must go through the egress proxy, whose allow list is the intent's `external_host`
entries plus the services kept at the source (`KEEP`). Every proxy decision is logged and read
back with `egress_records`.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

PROXY_NAME = "cmp-egress-proxy"
PROXY_PORT = 3128
# python:3.12.7-slim index digest (resolved from Docker Hub on 2026-11-05).
PROXY_IMAGE = "docker.io/library/python@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d"
ALLOW_ANNOTATION = "cmp.sandbox/allow-sha256"
_INSTANCE = "app.kubernetes.io/instance"
_PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


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


def allowlist(
    intents: list[dict[str, Any]], keep_hosts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    entries = {
        (i["external_host"].lower(), int(i["port"])) for i in intents if i.get("external_host")
    } | {(h["host"].lower(), int(h["port"])) for h in keep_hosts}
    return [{"host": host, "port": port} for host, port in sorted(entries)]


def _selector(release: str) -> dict[str, Any]:
    return {"matchLabels": {_INSTANCE: release}}


def _policy(namespace: str, name: str, labels: dict[str, str], spec: dict[str, Any]) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": spec,
    }


def _policy_name(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]
    return f"cmp-allow-{digest}"


def build_manifests(
    *,
    namespace: str,
    labels: dict[str, str],
    resources: list[dict[str, Any]],
    releases: dict[str, str],
    intents: list[dict[str, Any]],
    allow: list[dict[str, Any]],
    proxy_script: str,
) -> list[dict[str, Any]]:
    apps = [r for r in resources if r["kind"] == "APPLICATION"]
    manifests: list[dict[str, Any]] = [
        _policy(namespace, "cmp-default-deny", labels,
                {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
        _policy(namespace, "cmp-allow-dns", labels, {
            "podSelector": {}, "policyTypes": ["Egress"],
            "egress": [{
                "to": [{
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }],
                "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
            }],
        }),
    ]  # fmt: skip

    for intent in intents:
        if not intent.get("destination_resource_id"):
            continue
        source = releases[intent["source_resource_id"]]
        destination = releases[intent["destination_resource_id"]]
        ports = [{"protocol": intent.get("protocol", "TCP"), "port": intent["port"]}]
        name = _policy_name(source, destination, str(intent["port"]), ports[0]["protocol"])
        manifests.append(_policy(namespace, name, labels, {
            "podSelector": _selector(source), "policyTypes": ["Egress"],
            "egress": [{"to": [{"podSelector": _selector(destination)}], "ports": ports}],
        }))  # fmt: skip
        manifests.append(_policy(namespace, name + "-in", labels, {
            "podSelector": _selector(destination), "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"podSelector": _selector(source)}], "ports": ports}],
        }))  # fmt: skip

    # Test runners and ingress reach only the application port.
    for app in apps:
        manifests.append(_policy(namespace, _policy_name("entry", app["release_name"]), labels, {
            "podSelector": _selector(app["release_name"]), "policyTypes": ["Ingress"],
            "ingress": [{"ports": [{"protocol": "TCP", "port": app["port"]}]}],
        }))  # fmt: skip

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

    allow_json = json.dumps({"allow": allow}, sort_keys=True)
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
