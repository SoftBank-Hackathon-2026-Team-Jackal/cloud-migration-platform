"""Assemble scan results into requirements, env exceptions and prechecks (AV-006).

Values the sources do not declare are filled with the named defaults below and listed in
`assumptions`; nothing is chosen silently. Secret values are never read here.
"""

import hashlib
import json
import math
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path

from cmp_contracts.analysis import EnvExceptions, PrecheckReport
from cmp_contracts.provisioning_v2 import (
    ApplicationRequirement,
    DatabaseIdentity,
    NetworkIntent,
    PostgresRequirement,
    RequirementsV2,
    ResourceOutputBinding,
)
from pydantic import BaseModel, ConfigDict

from cmp_scanners.code import CodeScan, _files, _read
from cmp_scanners.manifests import ManifestScan, Workload

DEFAULT_APP_CPU_MILLICORES = 250
DEFAULT_APP_MEMORY_MIB = 256
DEFAULT_DB_CPU_MILLICORES = 250
DEFAULT_DB_MEMORY_MIB = 512
DEFAULT_DB_STORAGE_GIB = 5
DEFAULT_APP_PORT = 8080
DEFAULT_POSTGRES_PORT = 5432
SUPPORTED_POSTGRES_MAJOR = "16"

_DB_PREFIX = r"(?:DB|DATABASE|POSTGRES(?:QL)?|PG)_?"
_BINDING_PATTERNS = {
    "host": re.compile(rf"^{_DB_PREFIX}HOST$", re.IGNORECASE),
    "port": re.compile(rf"^{_DB_PREFIX}PORT$", re.IGNORECASE),
    "name": re.compile(rf"^(?:{_DB_PREFIX}(?:NAME|DB|DATABASE)|PGDATABASE)$", re.IGNORECASE),
    "username": re.compile(rf"^{_DB_PREFIX}(?:USER|USERNAME)$", re.IGNORECASE),
    "password_secret_ref": re.compile(rf"^{_DB_PREFIX}PASSWORD$", re.IGNORECASE),
    # Single-variable apps (D-16): the platform builds the URL into a Secret and injects a reference.
    "connection_url_secret_ref": re.compile(rf"^{_DB_PREFIX}(?:URL|URI)$", re.IGNORECASE),
}
# key=value DSNs are a different format; the contract cannot express them.
_DSN_VARIABLE = re.compile(rf"^{_DB_PREFIX}DSN$", re.IGNORECASE)
_CLOUD_ADDRESS = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9.-]*\.amazonaws\.com\b")
_SESSION_LIBRARIES = re.compile(
    r"gorilla/sessions|alexedwards/scs|express-session|cookie-session|flask_session|"
    r"flask-session|django\.contrib\.sessions|SessionMiddleware|HttpSession"
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Block(_Model):
    feature: str
    reason: str
    evidence: str


class RequirementsBuild(_Model):
    requirements: RequirementsV2 | None
    blocks: list[Block]
    env_exceptions: EnvExceptions
    unmapped_items: list[str]
    assumptions: list[str]


class MissingCopy(_Model):
    source: str
    line: int


class DockerfileCheck(_Model):
    dockerfiles: list[str]
    primary: str | None
    missing_copy_paths: list[MissingCopy]


# ---------------------------------------------------------------- unit parsing


def _cpu_millicores(value: str | None) -> int | None:
    if not value:
        return None
    try:
        text = str(value)
        return max(1, round(float(text[:-1])) if text.endswith("m") else round(float(text) * 1000))
    except ValueError:
        return None


_BYTE_UNITS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "": 1,
}  # fmt: skip


def _bytes(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?", str(value))
    if not match:
        return None
    return int(float(match.group(1)) * _BYTE_UNITS[match.group(2) or ""])


def _memory_mib(value: str | None) -> int | None:
    size = _bytes(value)
    return None if size is None else max(1, math.ceil(size / 1024**2))


def _storage_gib(value: str | None) -> int | None:
    size = _bytes(value)
    return None if size is None else max(1, math.ceil(size / 1024**3))


def _resource_id(name: str, taken: set[str]) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", name).strip("-_") or "app"
    if not cleaned[0].isalpha():
        cleaned = f"app-{cleaned}"
    cleaned = cleaned[:44]
    candidate, n = cleaned, 2
    while candidate in taken:
        candidate, n = f"{cleaned}-{n}", n + 1
    taken.add(candidate)
    return candidate


def _first(*values):
    return next((v for v in values if v is not None), None)


# ---------------------------------------------------------- requirements build


def _health_path(workload: Workload) -> str | None:
    for container in workload.containers:
        for probe in (container.readiness_probe, container.liveness_probe):
            if probe and probe.startswith("http:"):
                return probe.split(":", 2)[1]
    return None


def _postgres_requirement(workload: Workload, image: str, manifests, taken, assumptions, blocks):
    ident = f"{workload.kind}/{workload.name}"
    tag = image.split("@")[0].split("/")[-1].partition(":")[2]
    match = re.match(r"(\d+)", tag)
    major = match.group(1) if match else None
    if major is None:
        assumptions.append(
            f"POSTGRES_VERSION_NOT_DECLARED: {ident} assumed {SUPPORTED_POSTGRES_MAJOR}"
        )
    elif major != SUPPORTED_POSTGRES_MAJOR:
        blocks.append(
            Block(
                feature=f"PostgreSQL 버전 {major}",
                reason=f"RequirementsV2는 PostgreSQL {SUPPORTED_POSTGRES_MAJOR}만 지원한다.",
                evidence=ident,
            )
        )
        return None
    container = next((c for c in workload.containers if c.image == image), workload.containers[0])
    literals = container.env_literals
    user = _first(literals.get("POSTGRES_USER"), literals.get("PGUSER"))
    if user is None:
        user = "postgres"
        assumptions.append(f"DB_USER_DEFAULTED: {ident} uses the image default 'postgres'")
    name = _first(literals.get("POSTGRES_DB"), literals.get("PGDATABASE"))
    if name is None:
        name = user
        assumptions.append(f"DB_NAME_DEFAULTED: {ident} uses the image default (user name)")
    claims = {c.name: c for c in manifests.volume_claims}
    claim_names = [r.name for r in manifests.references if r.source == ident and r.kind == "PVC"]
    storage = _first(*(_storage_gib(claims[n].storage) for n in claim_names if n in claims))
    res = container.resources
    cpu = _first(_cpu_millicores(res.cpu_request), _cpu_millicores(res.cpu_limit))
    memory = _first(_memory_mib(res.memory_request), _memory_mib(res.memory_limit))
    for label, value in (("STORAGE", storage), ("CPU", cpu), ("MEMORY", memory)):
        if value is None:
            assumptions.append(f"DB_{label}_NOT_DECLARED: {ident} uses the default")
    return PostgresRequirement(
        id=_resource_id(workload.name, taken),
        kind="POSTGRESQL",
        version=SUPPORTED_POSTGRES_MAJOR,
        extensions=[],
        database=DatabaseIdentity(name=name, username=user),
        cpu_millicores=cpu or DEFAULT_DB_CPU_MILLICORES,
        memory_mib=memory or DEFAULT_DB_MEMORY_MIB,
        storage_gib=storage or DEFAULT_DB_STORAGE_GIB,
    )


def _bindings(names: set[str], db_id: str | None, unmapped: list[str]):
    bindings: dict[str, ResourceOutputBinding] = {}
    if db_id is None:
        return bindings
    for name in sorted(names):
        for output, pattern in _BINDING_PATTERNS.items():
            if pattern.match(name):
                bindings[name] = ResourceOutputBinding(resource_id=db_id, output=output)
                break
        else:
            if _DSN_VARIABLE.match(name):
                unmapped.append(
                    f"DB_CONNECTION_DSN_VARIABLE: {name} is a key=value DSN; "
                    "RequirementsV2 env_bindings cannot express it"
                )
    return bindings


def _matching_resources(
    selector: Mapping[str, str],
    namespace: str | None,
    workloads: Mapping[str, Workload],
    resource_ids: Mapping[str, str],
) -> list[str]:
    if not selector:
        return []  # an empty selector means every pod; not a specific app-to-app intent
    return sorted(
        resource_ids[key]
        for key, workload in workloads.items()
        if key in resource_ids
        and all(workload.labels.get(k) == v for k, v in selector.items())
        and not (namespace and workload.namespace and workload.namespace != namespace)
    )


def build_requirements(
    manifests: ManifestScan,
    code: CodeScan,
    env_names: Mapping[str, Sequence[str]],
) -> RequirementsBuild:
    assumptions: list[str] = []
    unmapped: list[str] = []
    blocks: list[Block] = [
        Block(
            feature=f"PostgreSQL 확장 {e.name}",
            reason="앱이 확장을 직접 만든다. 현재 Planning은 확장 요구를 거절한다(D-14).",
            evidence=f"{e.file}:{e.line}",
        )
        for e in code.extension_creations
    ]
    reads = {s.target for s in code.call_sites if s.kind == "ENV_READ"}

    taken: set[str] = set()
    postgres_by_resource = {p.resource: p for p in manifests.postgres_images}
    workloads = {f"{w.kind}/{w.name}": w for w in manifests.workloads}
    # Names the manifests inject (ConfigMap keys, env entries) count as declared, like `.env` names.
    declared = {n for names in env_names.values() for n in names} | {
        n
        for key, w in workloads.items()
        if w.kind in {"Deployment", "StatefulSet"} and key not in postgres_by_resource
        for n in w.env_names
    }
    databases: list[PostgresRequirement] = []
    resource_ids: dict[str, str] = {}
    for resource, image in postgres_by_resource.items():
        built = _postgres_requirement(
            workloads[resource], image.image, manifests, taken, assumptions, blocks
        )
        if built:
            databases.append(built)
            resource_ids[resource] = built.id
    if len(databases) > 1:
        unmapped.append("MULTIPLE_POSTGRES: DB env variables are bound to the first database only")
    db = databases[0] if databases else None
    if db is None and not blocks and any(s.kind == "DB_DRIVER" for s in code.call_sites):
        unmapped.append(
            "DB_NOT_IN_CLUSTER: the app uses a database driver but no PostgreSQL Pod was found; "
            "the first cycle only supports a PostgreSQL Pod (RDS and other external databases are out of scope)"
        )

    bindings = _bindings(declared | reads, db.id if db else None, unmapped)
    apps: list[ApplicationRequirement] = []
    for resource, workload in workloads.items():
        if workload.kind not in {"Deployment", "StatefulSet"} or resource in postgres_by_resource:
            continue
        if not workload.containers:
            continue
        container = workload.containers[0]
        res = container.resources
        cpu = _first(_cpu_millicores(res.cpu_request), _cpu_millicores(res.cpu_limit))
        memory = _first(_memory_mib(res.memory_request), _memory_mib(res.memory_limit))
        health = _health_path(workload)
        port = container.ports[0] if container.ports else None
        for code_, value in (
            ("REPLICAS", workload.replicas), ("CPU", cpu), ("MEMORY", memory),
            ("PORT", port), ("HEALTH_PATH", health),
        ):  # fmt: skip
            if value is None:
                assumptions.append(f"{code_}_NOT_DECLARED: {resource} uses the default")
        app_id = _resource_id(workload.name, taken)
        resource_ids[resource] = app_id
        apps.append(
            ApplicationRequirement(
                id=app_id,
                kind="APPLICATION",
                replicas=workload.replicas or 1,
                cpu_millicores=cpu or DEFAULT_APP_CPU_MILLICORES,
                memory_mib=memory or DEFAULT_APP_MEMORY_MIB,
                port=port or DEFAULT_APP_PORT,
                health_path=health or "/",
                env_bindings=bindings,
            )
        )
    if not apps:
        unmapped.append("NO_APPLICATION_WORKLOAD: no Deployment or StatefulSet found for the app")

    intents: dict[tuple, NetworkIntent] = {}

    def add_intent(source: str, destination: str, port: int, protocol: str = "TCP") -> None:
        intents.setdefault(
            (source, destination, port, protocol),
            NetworkIntent(
                source_resource_id=source,
                destination_resource_id=destination,
                port=port,
                protocol=protocol,
            ),
        )

    if db and any(s.kind == "DB_DRIVER" for s in code.call_sites):
        db_workload = workloads[next(iter(postgres_by_resource))]
        db_port = next((p for c in db_workload.containers for p in c.ports), DEFAULT_POSTGRES_PORT)
        for app in apps:
            add_intent(app.id, db.id, db_port)
    for rule in manifests.network_rules:
        sources = _matching_resources(rule.source_selector, rule.namespace, workloads, resource_ids)
        destinations = _matching_resources(
            rule.destination_selector, rule.namespace, workloads, resource_ids
        )
        for source in sources:
            for destination in destinations:
                if source != destination:
                    add_intent(source, destination, rule.port, rule.protocol)
    assumptions.append("EXTERNAL_INTENT_DEFERRED: external host intents come from the network scan")

    requirements = None
    if apps and not blocks:
        requirements = RequirementsV2(
            contract_version="2",
            resources=[*apps, *databases],
            network_intent=list(intents.values()),
        )
    excluded = sorted(n for n in declared | reads if n.startswith("AWS_"))
    replaced = {name: f"{db.id}.{b.output}" for name, b in bindings.items()} if db else {}
    missing = sorted(n for n in reads - declared if n not in excluded and n not in replaced)
    return RequirementsBuild(
        requirements=requirements,
        blocks=blocks,
        env_exceptions=EnvExceptions(excluded=excluded, replaced=replaced, missing=missing),
        unmapped_items=unmapped,
        assumptions=assumptions,
    )


def serialize_requirements(requirements: RequirementsV2) -> tuple[bytes, str]:
    """Stable JSON bytes and their SHA-256 (`requirements_ref`)."""
    data = json.dumps(
        requirements.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return data, hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ Dockerfile


def _is_dockerfile(name: str) -> bool:
    lower = name.lower()
    return lower == "dockerfile" or lower.startswith("dockerfile.") or lower.endswith(".dockerfile")


def _instructions(text: str):
    number, buffer, start = 0, "", 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.strip().startswith("#") and not buffer:
            continue
        if not buffer:
            start = number
        buffer += raw.rstrip("\\") + " "
        if not raw.rstrip().endswith("\\"):
            yield start, buffer.strip()
            buffer = ""
    if buffer.strip():
        yield start, buffer.strip()


def _copy_sources(rest: str) -> list[str] | None:
    if rest.lstrip().startswith("["):
        try:
            tokens = [str(t) for t in json.loads(rest)]
        except json.JSONDecodeError:
            return None
    else:
        try:
            tokens = shlex.split(rest)
        except ValueError:
            return None
    flags = [t for t in tokens if t.startswith("--")]
    if any(f.startswith("--from") for f in flags):
        return None
    paths = [t for t in tokens if not t.startswith("--")]
    return paths[:-1] if len(paths) >= 2 else None


def check_dockerfile(workspace: Path | str) -> DockerfileCheck:
    root = Path(workspace)
    found = [rel for path, rel in _files(root) if _is_dockerfile(path.name)]
    if not found:
        return DockerfileCheck(dockerfiles=[], primary=None, missing_copy_paths=[])
    primary = "Dockerfile" if "Dockerfile" in found else min(found, key=lambda p: (p.count("/"), p))
    context = (root / primary).parent
    text, _ = _read(root / primary)
    missing: list[MissingCopy] = []
    for line, instruction in _instructions(text or ""):
        keyword, _, rest = instruction.partition(" ")
        if keyword.upper() not in {"COPY", "ADD"}:
            continue
        for source in _copy_sources(rest) or []:
            if "$" in source or "://" in source or source.startswith("<<"):
                continue
            target = Path(os.path.normpath(context / source.lstrip("/")))
            inside = target == context or context in target.parents
            exists = inside and (
                any(context.glob(os.path.relpath(target, context)))
                if any(ch in source for ch in "*?[")
                else target.exists()
            )
            if not exists:
                missing.append(MissingCopy(source=source, line=line))
    return DockerfileCheck(dockerfiles=sorted(found), primary=primary, missing_copy_paths=missing)


# -------------------------------------------------------------------- precheck


def precheck(workspace: Path | str, manifests: ManifestScan, code: CodeScan) -> PrecheckReport:
    root = Path(workspace)
    findings = [f"DB_EXTENSION: {e.name} ({e.file}:{e.line})" for e in code.extension_creations]
    database_resources = {p.resource for p in manifests.postgres_images}
    for workload in manifests.workloads:
        resource = f"{workload.kind}/{workload.name}"
        is_app = (
            workload.kind in {"Deployment", "StatefulSet"} and resource not in database_resources
        )
        if is_app and workload.containers and _health_path(workload) is None:
            findings.append(f"HEALTH_PATH_NOT_FOUND: {resource}")
    cloud, sessions = [], []
    for path, rel in _files(root):
        text, _ = _read(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in _CLOUD_ADDRESS.finditer(line):
                cloud.append(f"CLOUD_ADDRESS: {match.group(0)} ({rel}:{number})")
        if _SESSION_LIBRARIES.search(text):
            sessions.append(f"SESSION_STATE: {rel}")
    return PrecheckReport(findings=[*findings, *dict.fromkeys(cloud), *sessions])
