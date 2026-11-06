"""Migration scope selection on a source EKS cluster (ticket 56, ADR-005).

The platform reads the user's cluster through the source connection role only: STS AssumeRole,
`eks:DescribeCluster` for the endpoint and CA, then an EKS bearer token (a presigned STS
`GetCallerIdentity` URL bound to the cluster name) for read-only Kubernetes GETs. Kubernetes access
needs a cluster access entry (`infra/connections/eks-view-access.yaml`); without one the API
answers 401/403 and the platform reports `EKS_ACCESS_DENIED`.

Secrets: Secret and ConfigMap objects are never read. Candidates use only reference names and keys
from the Pod template (`valueFrom`, `envFrom`) and environment variable names; `env[].value` is
never looked at. Nothing here logs.
"""

import base64
import json
import re
import secrets
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlencode

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from cmp_api.connections import (
    _DENIED,
    AwsConnector,
    ConnectionFailure,
    _assume,
    _error_code,
    get_connection,
)
from cmp_api.db import Base

# Every AWS read this module makes; tests check the source role template already grants them.
REQUIRED_AWS_ACTIONS = (
    "eks:DescribeCluster",
    "elasticloadbalancing:DescribeLoadBalancers",
    "iam:ListRolePolicies",
    "iam:GetRolePolicy",
    "iam:ListAttachedRolePolicies",
    "iam:GetPolicy",
    "iam:GetPolicyVersion",
)
TOKEN_PREFIX = "k8s-aws-v1."
TOKEN_SECONDS = 60
KUBE_TIMEOUT_SECONDS = 10
PAGE_SIZE = 500

CLUSTER_NAME = r"^[0-9A-Za-z][A-Za-z0-9_-]{0,99}$"
NAMESPACE = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
WORKLOAD_NAME = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
WORKLOAD_KINDS = ("Deployment", "StatefulSet")
_PLURAL = {"Deployment": "deployments", "StatefulSet": "statefulsets"}
_SYSTEM_PREFIXES = ("kube-", "amazon-", "aws-")
_IRSA = "eks.amazonaws.com/role-arn"
_ALB_ANNOTATION = "alb.ingress.kubernetes.io/"
_POSTGRES_IMAGES = ("postgres", "postgresql", "postgis")
_REFERENCE_TYPES = {"Secret": ("secret", "SECRET"), "ConfigMap": ("configmap", "CONFIG_MAP")}
_RESOURCE_TYPES = {
    "s3": "S3",
    "sqs": "SQS",
    "sns": "SNS",
    "dynamodb": "DYNAMODB",
    "rds": "RDS",
    "elasticache": "ELASTICACHE",
    "secretsmanager": "SECRETS_MANAGER",
    "kms": "KMS",
    "kinesis": "KINESIS",
    "es": "OPENSEARCH",
}


class KubeNotFound(Exception):
    """The Kubernetes API answered 404 for one path (callers map it to their own code)."""


class KubeApi(Protocol):
    def get(self, path: str) -> dict: ...

    def list(self, path: str) -> list[dict]: ...


KubeFactory = Callable[[str, bytes, str], KubeApi]


# ------------------------------------------------------------------------------ storage


class ScopeAnalysisRecord(Base):
    __tablename__ = "scope_analyses"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    selection: Mapped[dict] = mapped_column(JSON, nullable=False)
    candidates: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class ScopeConfirmationRecord(Base):
    __tablename__ = "scope_confirmations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    included: Mapped[list] = mapped_column(JSON, nullable=False)
    excluded: Mapped[list] = mapped_column(JSON, nullable=False)
    confirmed_at: Mapped[int] = mapped_column(Integer, nullable=False)


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


# ------------------------------------------------------------------------------ EKS access


def eks_token(sts_client: Any, cluster_name: str) -> str:
    """aws-iam-authenticator token: presigned GetCallerIdentity with `x-k8s-aws-id` signed in."""

    def bind_cluster(request, **_):
        request.headers["x-k8s-aws-id"] = cluster_name

    events = sts_client.meta.events
    events.register("before-sign.sts.GetCallerIdentity", bind_cluster)
    try:
        url = sts_client.generate_presigned_url(
            "get_caller_identity", Params={}, ExpiresIn=TOKEN_SECONDS, HttpMethod="GET"
        )
    finally:
        events.unregister("before-sign.sts.GetCallerIdentity", bind_cluster)
    return TOKEN_PREFIX + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


class EksKubeApi:
    """Read-only HTTPS GETs against the cluster endpoint, trusting only the cluster CA."""

    def __init__(self, endpoint: str, ca_pem: bytes, token: str):
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._context = ssl.create_default_context(cadata=ca_pem.decode())

    def get(self, path: str) -> dict:
        request = urllib.request.Request(
            self._endpoint + path,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request, context=self._context, timeout=KUBE_TIMEOUT_SECONDS
            ) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ConnectionFailure("EKS_ACCESS_DENIED") from None
            if exc.code == 404:
                raise KubeNotFound(path) from None
            raise ConnectionFailure("EKS_API_FAILED") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise ConnectionFailure("EKS_API_FAILED") from None

    def list(self, path: str) -> list[dict]:
        items: list[dict] = []
        token = None
        while True:
            query = {"limit": PAGE_SIZE, **({"continue": token} if token else {})}
            page = self.get(f"{path}?{urlencode(query)}")
            items.extend(page.get("items", []))
            token = page.get("metadata", {}).get("continue")
            if not token:
                return items


def default_kube_factory(endpoint: str, ca_pem: bytes, token: str) -> KubeApi:
    return EksKubeApi(endpoint, ca_pem, token)


@dataclass
class OpenCluster:
    connection_id: str
    region: str
    cluster: str
    kube: KubeApi
    aws: Callable[[str], Any]  # assumed-role client builder for this region


def open_cluster(
    session: Session,
    connector: AwsConnector,
    kube_factory: KubeFactory,
    connection_id: str,
    region: str | None,
    cluster: str,
) -> OpenCluster:
    connection = get_connection(session, connection_id)
    if connection.kind != "AWS_SOURCE":
        raise ConnectionFailure("SOURCE_CONNECTION_REQUIRED")
    if connection.status != "CONNECTED" or not connection.role_arn:
        raise ConnectionFailure("CONNECTION_NOT_READY")
    region = region or connection.region
    credentials = _assume(connector, connection, region)
    try:
        described = connector.assumed_client("eks", region, credentials).describe_cluster(
            name=cluster
        )["cluster"]
    except ClientError as exc:
        code = _error_code(exc)
        if code == "ResourceNotFoundException":
            raise ConnectionFailure("CLUSTER_NOT_FOUND") from None
        raise ConnectionFailure(
            "EKS_ACCESS_DENIED" if code in _DENIED else "EKS_API_FAILED"
        ) from None
    except BotoCoreError:
        raise ConnectionFailure("EKS_API_FAILED") from None
    endpoint = described.get("endpoint")
    ca = (described.get("certificateAuthority") or {}).get("data")
    if not endpoint or not ca:
        raise ConnectionFailure("EKS_API_FAILED")  # cluster still creating, or no public endpoint
    token = eks_token(connector.assumed_client("sts", region, credentials), cluster)
    return OpenCluster(
        connection_id=connection.id,
        region=region,
        cluster=cluster,
        kube=kube_factory(endpoint, base64.b64decode(ca), token),
        aws=lambda service: connector.assumed_client(service, region, credentials),
    )


# ------------------------------------------------------------------------------ listing


def is_system_namespace(name: str) -> bool:
    return name.startswith(_SYSTEM_PREFIXES) or name.endswith("-system")


def _name(item: dict) -> str:
    return item.get("metadata", {}).get("name", "")


def list_namespaces(cluster: OpenCluster) -> list[str]:
    names = (_name(item) for item in cluster.kube.list("/api/v1/namespaces"))
    return sorted(name for name in names if name and not is_system_namespace(name))


def _require_namespace(cluster: OpenCluster, namespace: str) -> None:
    if is_system_namespace(namespace):
        raise ConnectionFailure("NAMESPACE_NOT_FOUND")
    try:
        cluster.kube.get(f"/api/v1/namespaces/{quote(namespace)}")
    except KubeNotFound:
        raise ConnectionFailure("NAMESPACE_NOT_FOUND") from None


def _apps(namespace: str, kind: str) -> str:
    return f"/apis/apps/v1/namespaces/{quote(namespace)}/{_PLURAL[kind]}"


def list_workloads(cluster: OpenCluster, namespace: str) -> list[dict]:
    _require_namespace(cluster, namespace)
    workloads = []
    for kind in WORKLOAD_KINDS:
        names = sorted(_name(item) for item in cluster.kube.list(_apps(namespace, kind)))
        workloads.extend({"kind": kind, "name": name} for name in names if name)
    return workloads


# ------------------------------------------------------------------------------ analysis


@dataclass
class _Candidate:
    candidate_id: str
    resource_type: str
    name: str
    arn: str | None
    suggested: bool
    evidence: list[dict] = field(default_factory=list)

    def add(self, kind: str, location: str) -> None:
        entry = {"kind": kind, "location": location}
        if entry not in self.evidence:
            self.evidence.append(entry)

    def view(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "resource_type": self.resource_type,
            "name": self.name,
            "arn": self.arn,
            "evidence": self.evidence,
            "suggested": self.suggested,
        }


class _Candidates:
    def __init__(self):
        self._items: dict[str, _Candidate] = {}

    def get(
        self,
        prefix: str,
        resource_type: str,
        name: str,
        arn: str | None,
        suggested: bool,
        *,
        key_name: str | None = None,
    ):
        slug = re.sub(r"[^a-z0-9]+", "-", (key_name or name).lower()).strip("-")[:80] or "unnamed"
        key = f"{prefix}-{slug}"
        if key not in self._items:
            self._items[key] = _Candidate(key, resource_type, name, arn, suggested)
        return self._items[key]

    def views(self) -> list[dict]:
        return [item.view() for item in self._items.values()]


def _pod_spec(workload: dict) -> dict:
    return workload.get("spec", {}).get("template", {}).get("spec", {}) or {}


def _pod_labels(workload: dict) -> dict:
    return workload.get("spec", {}).get("template", {}).get("metadata", {}).get("labels") or {}


def _containers(spec: dict) -> Iterable[dict]:
    yield from spec.get("initContainers") or []
    yield from spec.get("containers") or []


def _references(spec: dict) -> list[tuple[str, str, str | None, str | None, str]]:
    """(object kind, object name, env name, key, field) — names only, never values."""
    found = []
    for container in _containers(spec):
        for env in container.get("env") or []:
            source = env.get("valueFrom") or {}
            for ref_field, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
                ref = source.get(ref_field)
                if ref and ref.get("name"):
                    found.append((kind, ref["name"], env.get("name"), ref.get("key"), "env"))
        for env_from in container.get("envFrom") or []:
            for ref_field, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
                ref = env_from.get(ref_field)
                if ref and ref.get("name"):
                    found.append((kind, ref["name"], None, None, "envFrom"))
    return found


def _selects(selector: dict | None, labels: dict, *, empty_matches: bool) -> bool:
    """matchLabels only; a selector with matchExpressions is not evaluated (no false evidence)."""
    if selector is None:
        return False
    if selector.get("matchExpressions"):
        return False
    wanted = selector.get("matchLabels") or {}
    if not wanted:
        return empty_matches
    return all(labels.get(key) == value for key, value in wanted.items())


def _read_workload(cluster: OpenCluster, namespace: str, name: str, kind: str | None):
    for candidate_kind in (kind,) if kind else WORKLOAD_KINDS:
        try:
            path = f"{_apps(namespace, candidate_kind)}/{quote(name)}"
            return candidate_kind, cluster.kube.get(path)
        except KubeNotFound:
            continue
    raise ConnectionFailure("WORKLOAD_NOT_FOUND")


def _alb_evidence(ingress: dict) -> str | None:
    annotations = ingress.get("metadata", {}).get("annotations") or {}
    if ingress.get("spec", {}).get("ingressClassName") == "alb":
        return "ingressClassName alb"
    if annotations.get("kubernetes.io/ingress.class") == "alb":
        return "kubernetes.io/ingress.class alb"
    alb = sorted(key for key in annotations if key.startswith(_ALB_ANNOTATION))
    return alb[0] if alb else None


def _backend_services(ingress: dict) -> set[str]:
    spec = ingress.get("spec", {})
    names = set()
    default = (spec.get("defaultBackend") or {}).get("service") or {}
    if default.get("name"):
        names.add(default["name"])
    for rule in spec.get("rules") or []:
        for path in (rule.get("http") or {}).get("paths") or []:
            service = (path.get("backend") or {}).get("service") or {}
            if service.get("name"):
                names.add(service["name"])
    return names


def _load_balancers(cluster: OpenCluster) -> dict[str, str]:
    """DNSName (lower case) -> ARN. Failures drop only the ARN evidence."""
    found: dict[str, str] = {}
    try:
        elbv2 = cluster.aws("elbv2")
        marker = None
        while True:
            page = elbv2.describe_load_balancers(**({"Marker": marker} if marker else {}))
            for lb in page.get("LoadBalancers", []):
                if lb.get("DNSName") and lb.get("LoadBalancerArn"):
                    found[lb["DNSName"].lower()] = lb["LoadBalancerArn"]
            marker = page.get("NextMarker")
            if not marker:
                return found
    except (ClientError, BotoCoreError):
        return found


def _add_ingresses(cluster, namespace, labels, candidates: _Candidates) -> None:
    services = cluster.kube.list(f"/api/v1/namespaces/{quote(namespace)}/services")
    mine = {
        _name(service)
        for service in services
        if _selects(
            {"matchLabels": service.get("spec", {}).get("selector") or {}},
            labels,
            empty_matches=False,
        )
    }
    ingresses = cluster.kube.list(
        f"/apis/networking.k8s.io/v1/namespaces/{quote(namespace)}/ingresses"
    )
    balancers = None
    for ingress in ingresses:
        evidence = _alb_evidence(ingress)
        if evidence is None or not (_backend_services(ingress) & mine):
            continue
        hostnames = [
            entry.get("hostname", "").lower()
            for entry in ingress.get("status", {}).get("loadBalancer", {}).get("ingress") or []
            if entry.get("hostname")
        ]
        arn = None
        if hostnames:
            balancers = _load_balancers(cluster) if balancers is None else balancers
            arn = next((balancers[h] for h in hostnames if h in balancers), None)
        name = arn.split("/")[-2] if arn else _name(ingress)
        item = candidates.get("alb", "ALB", name, arn, True, key_name=_name(ingress))
        item.add("MANIFEST", f"Ingress {_name(ingress)} · {evidence}")
        if arn:
            matched = next(h for h in hostnames if balancers.get(h) == arn)
            item.add("CLOUD_RESOURCE", f"DescribeLoadBalancers · DNSName {matched}")


def _policy_document(raw: Any) -> dict:
    if isinstance(raw, str):
        return json.loads(unquote(raw))
    return raw or {}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str | dict) else list(value)


def _role_policies(iam: Any, role_name: str) -> list[tuple[str, dict]]:
    policies = []
    marker = None
    while True:
        page = iam.list_role_policies(RoleName=role_name, **({"Marker": marker} if marker else {}))
        for name in page.get("PolicyNames", []):
            document = iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]
            policies.append((name, _policy_document(document)))
        marker = page.get("Marker") if page.get("IsTruncated") else None
        if not marker:
            break
    while True:
        page = iam.list_attached_role_policies(
            RoleName=role_name, **({"Marker": marker} if marker else {})
        )
        for attached in page.get("AttachedPolicies", []):
            arn = attached["PolicyArn"]
            version = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
            document = iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"][
                "Document"
            ]
            policies.append((attached["PolicyName"], _policy_document(document)))
        marker = page.get("Marker") if page.get("IsTruncated") else None
        if not marker:
            return policies


def _resource(arn: str) -> tuple[str, str, str, str] | None:
    """(prefix, resource type, name, normalized ARN) for a concrete ARN; None for wildcards."""
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        return None
    service, resource = parts[2], parts[5]
    if service in ("iam", "sts"):
        return None  # permissions, not resources to migrate
    if service == "s3":  # object ARNs (`bucket/*`) fold into their bucket
        bucket = resource.split("/", 1)[0]
        if not bucket or "*" in bucket or "?" in bucket:
            return None
        return ("s3", "S3", bucket, f"arn:{parts[1]}:s3:::{bucket}")
    if "*" in arn or "?" in arn:
        return None
    name = re.split(r"[:/]", resource)[-1]
    resource_type = _RESOURCE_TYPES.get(service, service.upper())
    return (service, resource_type, name, arn) if name else None


def _add_irsa(cluster, namespace, spec, candidates: _Candidates) -> None:
    account = spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"
    try:
        service_account = cluster.kube.get(
            f"/api/v1/namespaces/{quote(namespace)}/serviceaccounts/{quote(account)}"
        )
    except KubeNotFound:
        return
    role_arn = (service_account.get("metadata", {}).get("annotations") or {}).get(_IRSA)
    if not role_arn or ":role/" not in role_arn:
        return
    role_name = role_arn.rsplit("/", 1)[-1]
    role = candidates.get("iam-role", "IAM_ROLE", role_name, role_arn, True)
    role.add("MANIFEST", f"ServiceAccount {account} · {_IRSA}")
    try:
        policies = _role_policies(cluster.aws("iam"), role_name)
    except (ClientError, BotoCoreError, KeyError, ValueError):
        return  # e.g. the role lives in another account; keep the role, drop policy evidence
    for policy_name, document in policies:
        used: dict[tuple, list[str]] = {}
        for statement in _as_list(document.get("Statement")):
            if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
                continue
            for arn in _as_list(statement.get("Resource")):
                found = _resource(arn) if isinstance(arn, str) else None
                if found:
                    actions = used.setdefault(found, [])
                    for action in _as_list(statement.get("Action")):
                        if action not in actions:
                            actions.append(action)
        for (prefix, resource_type, name, arn), actions in used.items():
            item = candidates.get(prefix, resource_type, name, arn, True)
            item.add("IAM_POLICY", f"{role_name} · {policy_name} · {', '.join(actions)}")


def _add_references(kind, name, spec, candidates: _Candidates) -> None:
    for object_kind, object_name, env_name, key, source in _references(spec):
        prefix, resource_type = _REFERENCE_TYPES[object_kind]
        item = candidates.get(prefix, resource_type, object_name, None, True)
        if source == "env":
            location = f"{kind} {name} env · {env_name} ({object_kind} {object_name} key {key})"
        else:
            location = f"{kind} {name} envFrom · {object_kind} {object_name}"
        item.add("CONFIG", location)


def _postgres_image(spec: dict) -> str | None:
    for container in _containers(spec):
        image = container.get("image") or ""
        repository = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
        if repository.startswith(_POSTGRES_IMAGES):
            return image
    return None


def _add_postgres(cluster, namespace, kind, name, spec, labels, candidates: _Candidates) -> None:
    statefulsets = cluster.kube.list(_apps(namespace, "StatefulSet"))
    if not statefulsets:
        return
    policies = cluster.kube.list(
        f"/apis/networking.k8s.io/v1/namespaces/{quote(namespace)}/networkpolicies"
    )
    my_refs = {(ref[0], ref[1]) for ref in _references(spec)}
    for statefulset in statefulsets:
        db_name = _name(statefulset)
        if kind == "StatefulSet" and db_name == name:
            continue
        db_spec = _pod_spec(statefulset)
        image = _postgres_image(db_spec)
        if image is None:
            continue
        db_labels = _pod_labels(statefulset)
        evidence = []
        for policy in policies:
            policy_spec = policy.get("spec", {})
            if not _selects(policy_spec.get("podSelector"), labels, empty_matches=True):
                continue
            for rule in policy_spec.get("egress") or []:
                peers = [
                    peer
                    for peer in rule.get("to") or []
                    if "namespaceSelector" not in peer
                    and _selects(peer.get("podSelector"), db_labels, empty_matches=False)
                ]
                if peers:
                    evidence.append(
                        f"NetworkPolicy {_name(policy)} · egress to StatefulSet {db_name}"
                    )
                    break
        shared = sorted(my_refs & {(ref[0], ref[1]) for ref in _references(db_spec)})
        config = [
            f"{object_kind} {object_name} referenced by {kind} {name} and StatefulSet {db_name}"
            for object_kind, object_name in shared
        ]
        item = candidates.get("postgresql", "POSTGRESQL", db_name, None, bool(evidence or config))
        item.add("MANIFEST", f"StatefulSet {db_name} · image {image}")
        for location in evidence:
            item.add("MANIFEST", location)
        for location in config:
            item.add("CONFIG", location)


def analyze_scope(
    session: Session,
    cluster: OpenCluster,
    *,
    namespace: str,
    workload: str,
    workload_kind: str | None,
) -> dict:
    _require_namespace(cluster, namespace)
    kind, manifest = _read_workload(cluster, namespace, workload, workload_kind)
    spec, labels = _pod_spec(manifest), _pod_labels(manifest)
    candidates = _Candidates()
    _add_ingresses(cluster, namespace, labels, candidates)
    _add_irsa(cluster, namespace, spec, candidates)
    _add_references(kind, workload, spec, candidates)
    _add_postgres(cluster, namespace, kind, workload, spec, labels, candidates)
    selection = {
        "connection_id": cluster.connection_id,
        "region": cluster.region,
        "cluster": cluster.cluster,
        "namespace": namespace,
        "workload": workload,
        "workload_kind": kind,
    }
    record = ScopeAnalysisRecord(
        id="sa-" + secrets.token_hex(8),
        connection_id=cluster.connection_id,
        selection=selection,
        candidates=candidates.views(),
        created_at=_now(),
    )
    session.add(record)
    session.commit()
    return {"analysis_id": record.id, "selection": selection, "candidates": record.candidates}


# ------------------------------------------------------------------------------ confirmation


def confirm_scope(session: Session, analysis_id: str, included: list[str]) -> dict:
    analysis = session.get(ScopeAnalysisRecord, analysis_id)
    if analysis is None:
        raise ConnectionFailure("ANALYSIS_NOT_FOUND")
    ids = [item["candidate_id"] for item in analysis.candidates]
    chosen = list(dict.fromkeys(included))
    if not chosen or any(item not in ids for item in chosen):
        raise ConnectionFailure("INVALID_SCOPE")
    record = ScopeConfirmationRecord(
        id="scope-" + secrets.token_hex(8),
        analysis_id=analysis_id,
        included=chosen,
        excluded=[item for item in ids if item not in chosen],
        confirmed_at=_now(),
    )
    session.add(record)
    session.commit()
    return {
        "scope_id": record.id,
        "analysis_id": analysis_id,
        "included": record.included,
        "excluded": record.excluded,
        "confirmed_at": datetime.fromtimestamp(record.confirmed_at, UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    }
