"""Migration Network Preflight verdicts (MNP-002~006, decisions N1·N4·N5).

`run_network_preflight(request, executor)` runs the static CIDR check, then asks an on-site
executor to probe the real path once, and turns its observations into PASS/FAIL/UNKNOWN checks.
`may_start_data_migration` is the single gate Data Migration calls before the initial copy/CDC.
Nothing here performs I/O; the executor owns it.
"""

from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from typing import Protocol

from cmp_contracts.network_preflight import (
    NetworkPreflightRequest,
    NetworkPreflightResult,
    PreflightCheck,
    ProbeLocation,
    ProbeObservation,
)

from cmp_providers.artifacts import sha256_json

LIVE_CHECKS = ("check_route", "probe_db_path", "probe_large_transfer")
KIB_64 = 64 * 1024


class ProbeLocationUnavailable(Exception):
    """The replication location (Agent/K3s) could not be reached or used for the probe.

    `code` names why (UNKNOWN on every live check): `PROBE_LOCATION_UNAVAILABLE` by default,
    or an Agent-path code such as `AGENT_OFFLINE` (decision N3)."""

    def __init__(self, code: str = "PROBE_LOCATION_UNAVAILABLE"):
        super().__init__(code)
        self.code = code


class ProbeExecutor(Protocol):
    def probe(self, request: NetworkPreflightRequest) -> ProbeObservation: ...


def input_fingerprint(request: NetworkPreflightRequest) -> str:
    """N4: any change to the path, endpoint, TLS, CIDRs, location or limits gives a new value."""
    return sha256_json(request.model_dump(mode="json"))


def check_cidr_overlap(request: NetworkPreflightRequest) -> PreflightCheck:
    """MNP-002. Only ranges that are actually routed are compared; Subnets inside the same AWS
    VPC list are expected to nest and are never compared with each other."""
    if not request.aws_vpc_cidrs or not request.onprem_routed_cidrs:
        return PreflightCheck(
            name="check_cidr_overlap", status="UNKNOWN", code="CIDR_INPUT_MISSING"
        )
    roles = (
        ("aws_vpc", request.aws_vpc_cidrs),
        ("onprem_routed", request.onprem_routed_cidrs),
        ("tunnel", request.tunnel_cidrs),
    )
    conflicts = []
    for i, (role_a, cidrs_a) in enumerate(roles):
        for role_b, cidrs_b in roles[i + 1 :]:
            for a in cidrs_a:
                for b in cidrs_b:
                    if IPv4Network(a).overlaps(IPv4Network(b)):
                        conflicts.append({"a": a, "a_role": role_a, "b": b, "b_role": role_b})
    if conflicts:
        return PreflightCheck(
            name="check_cidr_overlap",
            status="FAIL",
            code="CIDR_OVERLAP",
            evidence={"conflicts": conflicts},
        )
    return PreflightCheck(name="check_cidr_overlap", status="PASS")


def _allowed(ip: str, cidrs: list[str]) -> bool:
    try:
        address = IPv4Address(ip)
    except ValueError:
        return False
    return any(address in IPv4Network(cidr) for cidr in cidrs)


def _route(request: NetworkPreflightRequest, obs: ProbeObservation) -> PreflightCheck:
    """MNP-003. A public answer or a path outside the expected tunnel is never PASS."""
    path = request.expected_network_path
    route = obs.node_route
    evidence = {"resolved_ip": obs.resolved_ip}
    if route is not None:
        evidence |= {"dev": route.dev, "via": route.via, "src": route.src}

    def check(status, code=None):
        return PreflightCheck(name="check_route", status=status, code=code, evidence=evidence)

    if obs.resolved_ip is None:
        return check("UNKNOWN", "ROUTE_UNVERIFIED")
    if not _allowed(obs.resolved_ip, path.allowed_private_destination_cidrs):
        return check("FAIL", "DESTINATION_NOT_ALLOWED")
    if route is None:
        return check("UNKNOWN", "ROUTE_UNVERIFIED")
    if route.dev is None:
        return check("FAIL", "NO_ROUTE")
    if path.tunnel_interface and route.dev != path.tunnel_interface:
        return check("FAIL", "UNEXPECTED_PATH")
    if path.tunnel_gateway and route.via != path.tunnel_gateway:
        return check("FAIL", "UNEXPECTED_PATH")
    return check("PASS")


def _db(obs: ProbeObservation) -> PreflightCheck:
    """MNP-004. Success proves a plain SQL read only, not replication permission or slots."""
    if obs.db_stage_failed:
        return PreflightCheck(name="probe_db_path", status="FAIL", code=obs.db_stage_failed)
    return PreflightCheck(
        name="probe_db_path", status="PASS", evidence={"resolved_ip": obs.resolved_ip}
    )


def _transfer(request: NetworkPreflightRequest, obs: ProbeObservation) -> PreflightCheck:
    """MNP-005. A completed large read is the evidence; MTU/ICMP results are auxiliary only."""
    mtu = (obs.mtu or {}).get("probe")
    if obs.db_stage_failed or obs.transfer is None:
        return PreflightCheck(
            name="probe_large_transfer", status="UNKNOWN", code="NOT_RUN", evidence={"mtu": mtu}
        )
    t = obs.transfer
    evidence = {
        "bytes_expected": request.transfer_bytes,
        "bytes_received": t.bytes,
        "seconds": round(t.seconds, 3),
        "stopped_after_64kib": t.status != "COMPLETE" and t.bytes > KIB_64,
        "mtu": mtu,
    }
    if t.status == "INTERRUPTED":
        code = "TRANSFER_INTERRUPTED"
    elif t.status == "TIMEOUT":
        code = "TRANSFER_TIMEOUT"
    elif t.bytes < request.transfer_bytes:
        code = "TRANSFER_SHORT"
    else:
        return PreflightCheck(name="probe_large_transfer", status="PASS", evidence=evidence)
    return PreflightCheck(name="probe_large_transfer", status="FAIL", code=code, evidence=evidence)


def _overall(checks: list[PreflightCheck]) -> str:
    states = {check.status for check in checks}
    return "FAIL" if "FAIL" in states else "UNKNOWN" if "UNKNOWN" in states else "PASS"


def run_network_preflight(
    request: NetworkPreflightRequest, executor: ProbeExecutor, *, now: datetime | None = None
) -> NetworkPreflightResult:
    """MNP-006 public entry point. Every required check must PASS for an overall PASS."""
    cidr = check_cidr_overlap(request)
    node = request.probe_target.node_name
    if cidr.status == "FAIL":
        live = [
            PreflightCheck(name=name, status="UNKNOWN", code="NOT_RUN_AFTER_STATIC_FAILURE")
            for name in LIVE_CHECKS
        ]
    else:
        try:
            obs = executor.probe(request)
        except ProbeLocationUnavailable as exc:
            live = [
                PreflightCheck(name=name, status="UNKNOWN", code=exc.code) for name in LIVE_CHECKS
            ]
        else:
            node = obs.node or node
            live = [_route(request, obs), _db(obs), _transfer(request, obs)]
    checks = [cidr, *live]
    target = request.probe_target
    return NetworkPreflightResult(
        status=_overall(checks),
        checks=checks,
        migration_id=request.migration_id,
        tested_endpoint=f"{request.source_db_host}:{request.source_db_port}",
        probe_location=ProbeLocation(
            agent_id=target.agent_id,
            cluster_uid=target.cluster_uid,
            namespace=target.namespace,
            node=node,
        ),
        checked_at=now or datetime.now(UTC),
        input_fingerprint=input_fingerprint(request),
    )


def may_start_data_migration(
    result: NetworkPreflightResult, request: NetworkPreflightRequest
) -> tuple[bool, list[str]]:
    """The gate before initial copy/CDC: overall PASS *for this exact request* (N4)."""
    blocked = []
    if result.status != "PASS":
        blocked.append("PREFLIGHT_NOT_PASSED")
    if result.input_fingerprint != input_fingerprint(request):
        blocked.append("PREFLIGHT_INPUT_CHANGED")
    return not blocked, blocked
