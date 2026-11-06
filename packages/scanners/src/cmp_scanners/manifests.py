"""Read Kubernetes manifests (AV-004). Secret values are never kept."""

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from cmp_scanners.sslmode import env_mode, modes_in

_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}
_BUILTIN_GROUPS = {
    "apps", "batch", "policy", "autoscaling", "networking.k8s.io", "storage.k8s.io",
    "rbac.authorization.k8s.io", "admissionregistration.k8s.io", "scheduling.k8s.io",
    "coordination.k8s.io", "discovery.k8s.io", "node.k8s.io", "events.k8s.io",
    "certificates.k8s.io", "flowcontrol.apiserver.k8s.io", "apiextensions.k8s.io",
}  # fmt: skip
_KNOWN_ANNOTATION_PREFIXES = (
    "alb.ingress.kubernetes.io/",
    "eks.amazonaws.com/",
    "kubernetes.io/",
    "kubectl.kubernetes.io/",
    "deployment.kubernetes.io/",
    "meta.helm.sh/",
    "helm.sh/",
    "app.kubernetes.io/",
    "storageclass.kubernetes.io/",
    "volume.beta.kubernetes.io/",
)
_EBS_PROVISIONERS = {"ebs.csi.aws.com", "kubernetes.io/aws-ebs"}
# Non-secret literals worth keeping: they identify the database; everything else is dropped.
_ENV_LITERAL_KEYS = {"POSTGRES_DB", "POSTGRES_USER", "PGDATABASE", "PGUSER"}


class ManifestScanError(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContainerResources(_Model):
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None


class ContainerInfo(_Model):
    name: str
    image: str
    ports: list[int]
    resources: ContainerResources
    readiness_probe: str | None
    liveness_probe: str | None
    evidence: str
    env_literals: dict[str, str] = {}


class Workload(_Model):
    kind: str
    name: str
    namespace: str | None
    replicas: int | None
    containers: list[ContainerInfo]
    evidence: str
    labels: dict[str, str] = {}
    env_names: list[str] = []


class CloudDependency(_Model):
    kind: Literal["ALB_INGRESS", "IRSA", "EBS_STORAGE_CLASS", "AWS_ONLY_SERVICE"]
    resource: str
    evidence: str


class PostgresImage(_Model):
    resource: str
    image: str
    digest: str | None
    needs_lookup: bool
    evidence: str


class Unconvertible(_Model):
    kind: Literal["SIDECAR", "INIT_CONTAINER", "CRD", "CUSTOM_RESOURCE", "UNKNOWN_ANNOTATION"]
    resource: str
    detail: str
    evidence: str


class SecretInfo(_Model):
    name: str
    namespace: str | None
    keys: list[str]
    evidence: str


class VolumeClaim(_Model):
    name: str
    storage: str | None
    storage_class: str | None
    evidence: str


class ConfigMapInfo(_Model):
    name: str
    namespace: str | None
    keys: list[str]
    evidence: str


class NetworkRule(_Model):
    policy: str
    namespace: str | None
    source_selector: dict[str, str]
    destination_selector: dict[str, str]
    port: int
    protocol: str
    evidence: str


class IngressInfo(_Model):
    """An Ingress: the paths it routes and whether it terminates TLS (ticket 28-B)."""

    name: str
    namespace: str | None
    tls: bool  # `spec.tls`, or an ALB certificate annotation
    paths: list[str]
    evidence: str


class IngressPolicy(_Model):
    """A NetworkPolicy for ingress: which pods it selects and who may reach them (ticket 28-B).

    `allow_all` is true when some rule lets any source in (no `from`, an empty selector, or
    0.0.0.0/0). No sources and not `allow_all` means ingress is denied."""

    policy: str
    namespace: str | None
    target_selector: dict[str, str]
    source_selectors: list[dict[str, str]]
    allow_all: bool
    evidence: str


class SslModeFinding(_Model):
    """A `sslmode` mode word found in a ConfigMap value or a literal environment value."""

    mode: str
    evidence: str


class Reference(_Model):
    source: str
    kind: Literal["ConfigMap", "Secret", "PVC"]
    name: str


class Unparsed(_Model):
    path: str
    reason: str


class ManifestScan(_Model):
    workloads: list[Workload]
    cloud_dependencies: list[CloudDependency]
    postgres_images: list[PostgresImage]
    unconvertible: list[Unconvertible]
    secrets: list[SecretInfo]
    references: list[Reference]
    volume_claims: list[VolumeClaim]
    configmaps: list[ConfigMapInfo]
    network_rules: list[NetworkRule]
    unparsed: list[Unparsed]
    # Ticket 28-B: security signals. Defaults keep older stored scans readable.
    ingresses: list[IngressInfo] = []
    ingress_policies: list[IngressPolicy] = []
    ssl_modes: list[SslModeFinding] = []


def _files(path: Path) -> list[tuple[Path, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        return [(path, path.name)]
    found = [*path.rglob("*.yaml"), *path.rglob("*.yml")]
    return [(f, f.relative_to(path).as_posix()) for f in sorted(found)]


def _probe(spec: dict[str, Any] | None) -> str | None:
    if not spec:
        return None
    if "httpGet" in spec:
        return f"http:{spec['httpGet'].get('path', '/')}:{spec['httpGet'].get('port')}"
    if "tcpSocket" in spec:
        return f"tcp:{spec['tcpSocket'].get('port')}"
    return "exec" if "exec" in spec else "other"


def _pod_path(kind: str) -> tuple[str, ...]:
    if kind == "Pod":
        return ("spec",)
    if kind == "CronJob":
        return ("spec", "jobTemplate", "spec", "template", "spec")
    return ("spec", "template", "spec")


def _dig(doc: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    node: Any = doc
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _is_postgres(image: str) -> bool:
    name = image.split("@")[0].split("/")[-1].split(":")[0].lower()
    return "postgres" in name and "exporter" not in name


def _scan_workload(doc, rel, resource, evidence, out) -> None:
    kind = doc["kind"]
    path = _pod_path(kind)
    pod = _dig(doc, path)
    pod_evidence = f"{evidence}/{'/'.join(path)}"
    containers = []
    for index, c in enumerate(pod.get("containers") or []):
        res = c.get("resources") or {}
        requests, limits = res.get("requests") or {}, res.get("limits") or {}
        info = ContainerInfo(
            name=c.get("name", ""),
            image=c.get("image", ""),
            ports=[p["containerPort"] for p in c.get("ports") or [] if "containerPort" in p],
            resources=ContainerResources(
                cpu_request=requests.get("cpu"),
                cpu_limit=limits.get("cpu"),
                memory_request=requests.get("memory"),
                memory_limit=limits.get("memory"),
            ),
            readiness_probe=_probe(c.get("readinessProbe")),
            liveness_probe=_probe(c.get("livenessProbe")),
            evidence=f"{pod_evidence}/containers/{index}",
            env_literals={
                e["name"]: str(e["value"])
                for e in c.get("env") or []
                if e.get("name") in _ENV_LITERAL_KEYS and "value" in e
            },
        )
        containers.append(info)
        for entry in c.get("env") or []:
            value = entry.get("value")
            name = entry.get("name", "")
            if not isinstance(value, str):
                continue  # a reference (valueFrom) or a number: its value is not here to read
            modes = [m] if (m := env_mode(name, value)) else modes_in(value)
            for mode in modes:
                out["ssl_modes"].append(
                    SslModeFinding(mode=mode, evidence=f"{info.evidence}/env/{name}")
                )
        if index > 0:
            out["unconvertible"].append(
                Unconvertible(
                    kind="SIDECAR", resource=resource, detail=info.name, evidence=info.evidence
                )
            )
        if kind in {"Deployment", "StatefulSet"} and _is_postgres(info.image):
            digest = info.image.split("@", 1)[1] if "@" in info.image else None
            out["postgres_images"].append(
                PostgresImage(
                    resource=resource,
                    image=info.image,
                    digest=digest,
                    needs_lookup=digest is None,
                    evidence=info.evidence,
                )
            )
    for index, c in enumerate(pod.get("initContainers") or []):
        out["unconvertible"].append(
            Unconvertible(
                kind="INIT_CONTAINER",
                resource=resource,
                detail=c.get("name", ""),
                evidence=f"{pod_evidence}/initContainers/{index}",
            )
        )
    _references(resource, pod, out)
    template_meta = (
        _dig(doc, path[:-1] + ("metadata",)) if kind != "Pod" else doc.get("metadata") or {}
    )
    labels = {str(k): str(v) for k, v in (template_meta.get("labels") or {}).items()}
    env_names = sorted(
        {e["name"] for c in pod.get("containers") or [] for e in c.get("env") or [] if "name" in e}
    )
    from_configmaps = [
        src["configMapRef"].get("name", "")
        for c in pod.get("containers") or []
        for src in c.get("envFrom") or []
        if "configMapRef" in src
    ]
    from_secrets = [
        src["secretRef"].get("name", "")
        for c in pod.get("containers") or []
        for src in c.get("envFrom") or []
        if "secretRef" in src
    ]
    out["_env"][resource] = (env_names, from_configmaps, from_secrets)
    for index, template in enumerate((doc.get("spec") or {}).get("volumeClaimTemplates") or []):
        claim_name = (template.get("metadata") or {}).get("name", "")
        _add_claim(template, claim_name, f"{evidence}/spec/volumeClaimTemplates/{index}", out)
        out["references"].append(Reference(source=resource, kind="PVC", name=claim_name))
    meta = doc.get("metadata") or {}
    replicas = _dig(doc, ("spec",)).get("replicas")
    out["workloads"].append(
        Workload(
            kind=kind,
            name=meta.get("name", ""),
            namespace=meta.get("namespace"),
            replicas=replicas if isinstance(replicas, int) else None,
            containers=containers,
            evidence=evidence,
            labels=labels,
        )
    )


def _add_claim(doc: dict[str, Any], name: str, evidence: str, out) -> None:
    spec = doc.get("spec") or {}
    storage = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
    out["volume_claims"].append(
        VolumeClaim(
            name=name,
            storage=str(storage) if storage is not None else None,
            storage_class=spec.get("storageClassName"),
            evidence=evidence,
        )
    )


def _scan_network_policy(doc: dict[str, Any], meta: dict[str, Any], evidence: str, out) -> None:
    spec = doc.get("spec") or {}
    if "Egress" not in (spec.get("policyTypes") or []):
        return
    source = (spec.get("podSelector") or {}).get("matchLabels") or {}
    for rule_index, rule in enumerate(spec.get("egress") or []):
        for peer in rule.get("to") or []:
            if "podSelector" not in peer or "namespaceSelector" in peer or "ipBlock" in peer:
                continue  # DNS, other namespaces and CIDR rules are not app-to-app intents
            destination = (peer.get("podSelector") or {}).get("matchLabels") or {}
            for port in rule.get("ports") or []:
                number = port.get("port")
                protocol = port.get("protocol", "TCP")
                if isinstance(number, int) and protocol in {"TCP", "UDP"}:
                    out["network_rules"].append(
                        NetworkRule(
                            policy=meta.get("name", ""),
                            namespace=meta.get("namespace"),
                            source_selector={str(k): str(v) for k, v in source.items()},
                            destination_selector={str(k): str(v) for k, v in destination.items()},
                            port=number,
                            protocol=protocol,
                            evidence=f"{evidence}/spec/egress/{rule_index}",
                        )
                    )


_ANY_SOURCE = {"0.0.0.0/0", "::/0"}


def _labels(selector: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in ((selector or {}).get("matchLabels") or {}).items()}


def _scan_ingress(
    doc: dict[str, Any], meta: dict[str, Any], annotations: dict[str, Any], evidence: str, out
) -> None:
    spec = doc.get("spec") or {}
    paths: list[str] = []
    for rule in spec.get("rules") or []:
        for item in (rule.get("http") or {}).get("paths") or []:
            path = item.get("path") or "/"
            if path not in paths:
                paths.append(path)
    if not paths and spec.get("defaultBackend"):
        paths.append("/")
    out["ingresses"].append(
        IngressInfo(
            name=meta.get("name", ""),
            namespace=meta.get("namespace"),
            tls=bool(spec.get("tls")) or "alb.ingress.kubernetes.io/certificate-arn" in annotations,
            paths=paths,
            evidence=evidence,
        )
    )


def _scan_ingress_policy(doc: dict[str, Any], meta: dict[str, Any], evidence: str, out) -> None:
    spec = doc.get("spec") or {}
    types = spec.get("policyTypes") or []
    if types and "Ingress" not in types:
        return  # no policyTypes means Kubernetes applies ingress rules
    sources: list[dict[str, str]] = []
    allow_all = False
    for rule in spec.get("ingress") or []:
        peers = rule.get("from") or []
        if not peers:
            allow_all = True  # a rule without `from` lets every source in
        for peer in peers:
            if "ipBlock" in peer:
                allow_all = allow_all or (peer["ipBlock"] or {}).get("cidr") in _ANY_SOURCE
            elif "namespaceSelector" in peer:
                labels = _labels(peer["namespaceSelector"])
                if labels:
                    sources.append({f"namespace.{k}": v for k, v in labels.items()})
                else:
                    allow_all = True  # every namespace
            elif "podSelector" in peer:
                labels = _labels(peer["podSelector"])
                if labels:
                    sources.append(labels)
                else:
                    allow_all = True  # every pod of the namespace
    out["ingress_policies"].append(
        IngressPolicy(
            policy=meta.get("name", ""),
            namespace=meta.get("namespace"),
            target_selector=_labels(spec.get("podSelector")),
            source_selectors=sources,
            allow_all=allow_all,
            evidence=evidence,
        )
    )


def _references(resource: str, pod: dict[str, Any], out) -> None:
    found: set[tuple[str, str]] = set()
    for c in [*(pod.get("containers") or []), *(pod.get("initContainers") or [])]:
        for source in c.get("envFrom") or []:
            if "configMapRef" in source:
                found.add(("ConfigMap", source["configMapRef"].get("name", "")))
            if "secretRef" in source:
                found.add(("Secret", source["secretRef"].get("name", "")))
        for env in c.get("env") or []:
            ref = env.get("valueFrom") or {}
            if "configMapKeyRef" in ref:
                found.add(("ConfigMap", ref["configMapKeyRef"].get("name", "")))
            if "secretKeyRef" in ref:
                found.add(("Secret", ref["secretKeyRef"].get("name", "")))
    for volume in pod.get("volumes") or []:
        if "configMap" in volume:
            found.add(("ConfigMap", volume["configMap"].get("name", "")))
        if "secret" in volume:
            found.add(("Secret", volume["secret"].get("secretName", "")))
        if "persistentVolumeClaim" in volume:
            found.add(("PVC", volume["persistentVolumeClaim"].get("claimName", "")))
    for kind, name in sorted(found):
        out["references"].append(Reference(source=resource, kind=kind, name=name))


def _scan_document(doc: dict[str, Any], rel: str, out) -> None:
    kind, meta = doc["kind"], doc.get("metadata") or {}
    resource = f"{kind}/{meta.get('name', '')}"
    evidence = f"{rel}#{resource}"
    group = doc["apiVersion"].split("/")[0] if "/" in doc["apiVersion"] else ""
    annotations = meta.get("annotations") or {}

    for key in sorted(annotations):
        if key.startswith("alb.ingress.kubernetes.io/"):
            continue
        if not key.startswith(_KNOWN_ANNOTATION_PREFIXES):
            out["unconvertible"].append(
                Unconvertible(
                    kind="UNKNOWN_ANNOTATION",
                    resource=resource,
                    detail=key,
                    evidence=f"{evidence}/metadata/annotations/{key}",
                )
            )

    if group.endswith("services.k8s.aws"):
        out["cloud_dependencies"].append(
            CloudDependency(kind="AWS_ONLY_SERVICE", resource=resource, evidence=evidence)
        )
    elif kind == "CustomResourceDefinition":
        out["unconvertible"].append(
            Unconvertible(
                kind="CRD", resource=resource, detail=meta.get("name", ""), evidence=evidence
            )
        )
    elif group and group not in _BUILTIN_GROUPS and not group.endswith(".k8s.io"):
        out["unconvertible"].append(
            Unconvertible(
                kind="CUSTOM_RESOURCE", resource=resource, detail=group, evidence=evidence
            )
        )

    if kind == "Ingress":
        alb_key = next((k for k in annotations if k.startswith("alb.ingress.kubernetes.io/")), None)
        spec = doc.get("spec") or {}
        legacy = annotations.get("kubernetes.io/ingress.class") == "alb"
        if alb_key or legacy or spec.get("ingressClassName") == "alb":
            if alb_key:
                where = f"metadata/annotations/{alb_key}"
            elif legacy:
                where = "metadata/annotations/kubernetes.io/ingress.class"
            else:
                where = "spec/ingressClassName"
            out["cloud_dependencies"].append(
                CloudDependency(
                    kind="ALB_INGRESS", resource=resource, evidence=f"{evidence}/{where}"
                )
            )
    if kind == "Ingress":
        _scan_ingress(doc, meta, annotations, evidence, out)
    if kind == "ServiceAccount" and "eks.amazonaws.com/role-arn" in annotations:
        out["cloud_dependencies"].append(
            CloudDependency(
                kind="IRSA",
                resource=resource,
                evidence=f"{evidence}/metadata/annotations/eks.amazonaws.com/role-arn",
            )
        )
    if kind == "StorageClass" and doc.get("provisioner") in _EBS_PROVISIONERS:
        out["cloud_dependencies"].append(
            CloudDependency(
                kind="EBS_STORAGE_CLASS", resource=resource, evidence=f"{evidence}/provisioner"
            )
        )
    if kind == "ConfigMap":
        for key, value in sorted((doc.get("data") or {}).items()):
            for mode in modes_in(value) if isinstance(value, str) else []:
                out["ssl_modes"].append(
                    SslModeFinding(mode=mode, evidence=f"{evidence}/data/{key}")
                )
        keys = sorted({*(doc.get("data") or {}), *(doc.get("binaryData") or {})})
        out["configmaps"].append(
            ConfigMapInfo(
                name=meta.get("name", ""),
                namespace=meta.get("namespace"),
                keys=keys,
                evidence=evidence,
            )
        )
    if kind == "NetworkPolicy":
        _scan_network_policy(doc, meta, evidence, out)
        _scan_ingress_policy(doc, meta, evidence, out)
    if kind == "PersistentVolumeClaim":
        _add_claim(doc, meta.get("name", ""), evidence, out)
    if kind == "Secret":
        keys = sorted({*(doc.get("data") or {}), *(doc.get("stringData") or {})})
        out["secrets"].append(
            SecretInfo(
                name=meta.get("name", ""),
                namespace=meta.get("namespace"),
                keys=keys,
                evidence=evidence,
            )
        )
    if kind in _WORKLOAD_KINDS:
        _scan_workload(doc, rel, resource, evidence, out)


_RESULT_KEYS = (
    "workloads",
    "cloud_dependencies",
    "postgres_images",
    "unconvertible",
    "secrets",
    "references",
    "volume_claims",
    "configmaps",
    "network_rules",
    "unparsed",
    "ingresses",
    "ingress_policies",
    "ssl_modes",
)


def scan_manifests(path: Path | str) -> ManifestScan:
    out: dict = {"_env": {}, **{key: [] for key in _RESULT_KEYS}}
    for file, rel in _files(Path(path)):
        try:
            documents = list(yaml.safe_load_all(file.read_text()))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            out["unparsed"].append(Unparsed(path=rel, reason=type(exc).__name__))
            continue
        for doc in documents:
            if isinstance(doc, dict) and "kind" in doc and "apiVersion" in doc:
                _scan_document(doc, rel, out)
    env = out.pop("_env")
    configmap_keys = {c.name: c.keys for c in out["configmaps"]}
    secret_keys = {sec.name: sec.keys for sec in out["secrets"]}
    filled = []
    for workload in out["workloads"]:
        names, from_maps, from_secrets = env.get(f"{workload.kind}/{workload.name}", ([], [], []))
        declared = {
            *names,
            *(k for n in from_maps for k in configmap_keys.get(n, [])),
            *(k for n in from_secrets for k in secret_keys.get(n, [])),
        }
        filled.append(workload.model_copy(update={"env_names": sorted(declared)}))
    out["workloads"] = filled
    return ManifestScan(**out)


def render_helm_chart(
    chart_dir: Path | str,
    *,
    release: str,
    helm_bin: str = "helm",
    values_file: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> Path:
    """Run `helm template` and write the rendered YAML into `output_dir/rendered.yaml`."""
    command = [helm_bin, "template", release, str(chart_dir)]
    if values_file:
        command += ["-f", str(values_file)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestScanError(f"helm could not run: {type(exc).__name__}") from exc
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        raise ManifestScanError(f"helm template failed: {(lines[-1] if lines else '')[:300]}")
    out = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="cmp-helm-"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "rendered.yaml").write_text(result.stdout)
    return out
