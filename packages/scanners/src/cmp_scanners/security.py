"""Measure the source's security state from what the scans read (ticket 28-B).

Four criteria, the demo's first cycle: external HTTPS, DB access scope, DB transport encryption and
admin API exposure. Each gets a state and the evidence behind it. The rules are about how things
are configured (a listener without HTTPS, a database no NetworkPolicy protects), never about one
app: no path, port or name of a particular application is written here.

Without evidence the state is UNKNOWN. UNKNOWN is never protection: it asks for a person.
"""

from collections.abc import Iterable, Sequence

from cmp_contracts.analysis import Evidence, SecurityMeasure

from cmp_scanners.aws_infra import AwsInfraScan
from cmp_scanners.code import CodeScan
from cmp_scanners.manifests import ManifestScan
from cmp_scanners.routes import HttpRoute
from cmp_scanners.sslmode import ENCRYPTED_MODES, PLAINTEXT_MODES

DB_PORT = 5432
_INTERNET = {"0.0.0.0/0", "::/0"}
_HTTPS_ONLY = {"redirect-to-https", "https-only"}
# Path segments that, by common convention, mark operational endpoints. They only make a route a
# candidate; a person confirms it (ticket 29).
ADMIN_SEGMENTS = frozenset(
    {"admin", "internal", "debug", "pprof", "actuator", "metrics", "management", "manage"}
)


def _evidence(kind: str, locator: str) -> Evidence:
    return Evidence(kind=kind, locator=locator, verified=True)  # type: ignore[arg-type]


def _state(secure: int, weak: int) -> str:
    if secure and not weak:
        return "PROTECTED"
    if secure and weak:
        return "PARTIAL"
    return "UNPROTECTED"


def _measure(criterion, state, reasons: Iterable[str], evidence: Iterable[Evidence]):
    unique = {e.locator: e for e in evidence}
    return SecurityMeasure(
        criterion=criterion,
        state=state,
        reasons=sorted(set(reasons)),
        evidence=[unique[key] for key in sorted(unique)],
    )


# ------------------------------------------------------------------------------ external HTTPS


def _entry_points(infra: AwsInfraScan | None):
    """(secure, reason when weak, evidence) for each entry the AWS scan shows."""
    entries = []
    if infra is None:
        return entries
    for lb in infra.load_balancers:
        if lb.scheme != "internet-facing":
            continue
        for listener in lb.listeners:
            if listener.protocol in {"HTTPS", "TLS"}:
                secure = True
            elif listener.protocol == "HTTP":
                secure = listener.redirects_to_https
            else:
                continue  # plain TCP and others: nothing to say about HTTPS
            locator = f"elbv2:{lb.name}:{listener.port}"
            reason = None if secure else f"LISTENER_HTTP_NO_REDIRECT {lb.name}:{listener.port}"
            entries.append((secure, reason, _evidence("CLOUD_RESOURCE", locator)))
    for dist in infra.cloudfront if infra.cloudfront_status == "COLLECTED" else []:
        secure = (
            bool(dist.viewer_protocol_policies)
            and set(dist.viewer_protocol_policies) <= _HTTPS_ONLY
        )
        reason = None if secure else f"CLOUDFRONT_ALLOWS_HTTP {dist.id}"
        entries.append((secure, reason, _evidence("CLOUD_RESOURCE", f"cloudfront:{dist.id}")))
    return entries


def _external_https(manifests: ManifestScan | None, infra: AwsInfraScan | None) -> SecurityMeasure:
    entries = _entry_points(infra)
    if not entries and manifests is not None:
        entries = [
            (
                ing.tls,
                None if ing.tls else f"INGRESS_NO_TLS {ing.name}",
                _evidence("MANIFEST", ing.evidence),
            )
            for ing in manifests.ingresses
        ]
    extra = ["CLOUDFRONT_NOT_READ"] if infra and infra.cloudfront_status == "DENIED" else []
    if not entries:
        return _measure("EXTERNAL_HTTPS", "UNKNOWN", ["NO_EXTERNAL_ENTRY_VISIBLE", *extra], [])
    secure = sum(1 for e in entries if e[0])
    weak = len(entries) - secure
    return _measure(
        "EXTERNAL_HTTPS",
        _state(secure, weak),
        [*(r for _, r, _ in entries if r), *extra],
        (ev for _, _, ev in entries),
    )


# -------------------------------------------------------------------------------- DB access scope


def _selects(target: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in target.items())


def _db_access(manifests: ManifestScan | None, infra: AwsInfraScan | None) -> SecurityMeasure:
    verdicts: list[bool] = []
    reasons: list[str] = []
    evidence: list[Evidence] = []
    if manifests is not None:
        workloads = {f"{w.kind}/{w.name}": w for w in manifests.workloads}
        for db in manifests.postgres_images:
            workload = workloads.get(db.resource)
            if workload is None:
                continue
            name = workload.name
            selecting = [
                p
                for p in manifests.ingress_policies
                if p.namespace == workload.namespace
                and _selects(p.target_selector, workload.labels)
            ]
            if not selecting:
                verdicts.append(False)
                reasons.append(f"NO_INGRESS_NETWORK_POLICY {name}")
                evidence.append(_evidence("MANIFEST", db.evidence))
            elif any(p.allow_all for p in selecting):
                verdicts.append(False)
                reasons.append(f"NETWORK_POLICY_ALLOWS_ALL {name}")
                evidence.extend(_evidence("MANIFEST", p.evidence) for p in selecting)
            else:
                verdicts.append(True)
                evidence.extend(_evidence("MANIFEST", p.evidence) for p in selecting)
    for group in infra.security_groups if infra else []:
        for rule in group.ingress:
            covers = rule.protocol == "-1" or (
                rule.from_port is not None
                and rule.to_port is not None
                and rule.from_port <= DB_PORT <= rule.to_port
            )
            if covers and _INTERNET & set(rule.cidrs):
                verdicts.append(False)
                reasons.append(f"SG_DB_PORT_OPEN_TO_INTERNET {group.id}")
                evidence.append(_evidence("CLOUD_RESOURCE", f"ec2:{group.id}"))
    if not verdicts:
        return _measure("DB_ACCESS_SCOPE", "UNKNOWN", ["DB_NOT_FOUND_IN_MANIFESTS"], [])
    secure = sum(verdicts)
    return _measure("DB_ACCESS_SCOPE", _state(secure, len(verdicts) - secure), reasons, evidence)


# ------------------------------------------------------------------------ DB transport encryption


def _db_transport(manifests: ManifestScan | None, code: CodeScan | None) -> SecurityMeasure:
    found = [
        (f.mode, _evidence("MANIFEST", f.evidence))
        for f in (manifests.ssl_modes if manifests else [])
    ]
    found += [
        (s.mode, _evidence("CODE", f"{s.file}:{s.line}")) for s in (code.ssl_modes if code else [])
    ]
    if not found:
        return _measure("DB_TRANSPORT_ENCRYPTION", "UNKNOWN", ["SSLMODE_NOT_VISIBLE"], [])
    secure = sum(1 for mode, _ in found if mode in ENCRYPTED_MODES)
    weak = sum(1 for mode, _ in found if mode in PLAINTEXT_MODES)
    return _measure(
        "DB_TRANSPORT_ENCRYPTION",
        _state(secure, weak),
        (f"SSLMODE_PLAINTEXT {mode}" for mode, _ in found if mode in PLAINTEXT_MODES),
        (ev for _, ev in found),
    )


# --------------------------------------------------------------------------- admin API exposure


def _admin_candidates(routes: Sequence[HttpRoute]) -> list[HttpRoute]:
    return [
        r
        for r in routes
        if ADMIN_SEGMENTS & {segment.lower() for segment in r.path.split("/") if segment}
    ]


def _covered_by(path: str, prefix: str) -> bool:
    return prefix == "/" or path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _admin_api(
    manifests: ManifestScan | None, infra: AwsInfraScan | None, routes: Sequence[HttpRoute]
) -> SecurityMeasure:
    if not routes:
        return _measure("ADMIN_API_EXPOSURE", "UNKNOWN", ["NO_ROUTES_VISIBLE"], [])
    candidates = _admin_candidates(routes)
    if not candidates:
        return _measure("ADMIN_API_EXPOSURE", "NOT_APPLICABLE", [], [])
    ingresses = manifests.ingresses if manifests else []
    external = bool(_entry_points(infra)) or bool(ingresses)
    label = [f"ADMIN_PATH_CANDIDATE {c.method or 'ANY'} {c.path}" for c in candidates]
    proof = [_evidence("CODE", f"{c.file}:{c.line}") for c in candidates]
    if not external:
        return _measure(
            "ADMIN_API_EXPOSURE", "UNKNOWN", ["EXTERNAL_REACHABILITY_UNKNOWN", *label], proof
        )
    prefixes = [p for ing in ingresses for p in ing.paths]
    exposed = [
        (c, ev, text)
        for c, ev, text in zip(candidates, proof, label, strict=True)
        if not prefixes or any(_covered_by(c.path, p) for p in prefixes)
    ]
    if not exposed:
        return _measure("ADMIN_API_EXPOSURE", "PROTECTED", ["ADMIN_PATH_NOT_ROUTED"], proof)
    return _measure(
        "ADMIN_API_EXPOSURE",
        "UNPROTECTED",
        [*(text for _, _, text in exposed), "ADMIN_PATH_UNCONFIRMED"],
        (ev for _, ev, _ in exposed),
    )


# ---------------------------------------------------------------------------------------- entry


def measure_security(
    manifests: ManifestScan | None,
    infra: AwsInfraScan | None,
    code: CodeScan | None,
    routes: Sequence[HttpRoute],
) -> list[SecurityMeasure]:
    """All four measures, always in the same order, for the same inputs the same result."""
    return [
        _external_https(manifests, infra),
        _db_access(manifests, infra),
        _db_transport(manifests, code),
        _admin_api(manifests, infra, routes),
    ]
