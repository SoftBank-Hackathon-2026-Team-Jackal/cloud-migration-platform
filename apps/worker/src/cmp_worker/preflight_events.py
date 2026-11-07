"""`data.preflight` event from a `NetworkPreflightResult` (ticket 24 module, MNP-01).

The web shape (`cmp_contracts.events.DataPreflightData`) wants one evidence string per check and
the network path from the request. Only codes, addresses, interface names and counters are
used; the Secret reference and any credential never appear.
"""

from cmp_contracts.network_preflight import NetworkPreflightRequest, NetworkPreflightResult

from cmp_worker.events import emit_event

PHASE = "data_copy"
DB_STEPS = ("DNS", "TCP", "TLS", "AUTH", "QUERY")
STAGE_STEP = {
    "DNS_FAILED": "DNS",
    "TCP_UNREACHABLE": "TCP",
    "TLS_FAILED": "TLS",
    "AUTH_FAILED": "AUTH",
    "QUERY_FAILED": "QUERY",
}


def _evidence(name: str, evidence: dict) -> str:
    if name == "check_cidr_overlap":
        return "; ".join(
            f"{c['a']} ({c['a_role']}) overlaps {c['b']} ({c['b_role']})"
            for c in evidence.get("conflicts", [])
        )
    if name == "check_route":
        parts = [f"{key} {evidence[key]}" for key in ("dev", "via", "src") if evidence.get(key)]
        ip = evidence.get("resolved_ip")
        return " ".join(([f"to {ip}"] if ip else []) + parts)
    if name == "probe_db_path":
        return evidence.get("resolved_ip") or ""
    if name == "probe_large_transfer" and "bytes_received" in evidence:
        return (
            f"{evidence['bytes_received']}/{evidence['bytes_expected']} bytes "
            f"in {evidence['seconds']}s"
        )
    return ""


def _steps(status: str, code: str | None) -> list[dict]:
    if status == "PASS":
        return [{"name": step, "status": "PASS"} for step in DB_STEPS]
    failed = STAGE_STEP.get(code or "")
    if failed is None:
        return [{"name": step, "status": "UNKNOWN"} for step in DB_STEPS]
    index = DB_STEPS.index(failed)
    return [
        {"name": step, "status": "PASS" if i < index else "FAIL" if i == index else "PENDING"}
        for i, step in enumerate(DB_STEPS)
    ]


def preflight_event_data(
    request: NetworkPreflightRequest, result: NetworkPreflightResult, may_start: bool
) -> dict:
    checked_at = result.checked_at.isoformat().replace("+00:00", "Z")
    checks = []
    for check in result.checks:
        item = {
            "name": check.name,
            "status": check.status,
            "code": check.code,
            "evidence": _evidence(check.name, check.evidence),
            "checked_at": checked_at,
        }
        if check.name == "probe_db_path":
            item |= {
                "tested_endpoint": result.tested_endpoint,
                "steps": _steps(check.status, check.code),
            }
        if check.name == "probe_large_transfer":
            evidence = check.evidence
            if "bytes_received" in evidence:
                item |= {
                    "bytes_received": evidence["bytes_received"],
                    "bytes_expected": evidence["bytes_expected"],
                    "seconds": evidence["seconds"],
                }
            if evidence.get("mtu") in ("NOT_ATTEMPTED", "PASS", "FAIL"):
                item["mtu"] = evidence["mtu"]
        checks.append(item)
    location = result.probe_location
    return {
        "status": result.status,
        "checks": checks,
        "probe_location": _probe_location(location.agent_id, location.namespace, location.node),
        "input_fingerprint": result.input_fingerprint,
        "checked_at": checked_at,
        "may_start": may_start,
        "network": _network(
            request,
            source_addr=result.tested_endpoint,
            node=location.node,
            namespace=location.namespace,
            detail=f"{location.agent_id} ({location.cluster_uid})",
        ),
    }


def _probe_location(agent_id: str, namespace: str, node: str | None) -> str:
    return f"{agent_id}/{namespace}" + (f"@{node}" if node else "")


def _path(request: NetworkPreflightRequest) -> dict:
    """`wg*` on the probe node: that interface. No interface but a tunnel range: the data gateway
    (ADR-007 2안, ticket 76 W9) — the source DB address is the gateway Service, whose name is the
    first DNS label. Neither: `other`."""
    interface = request.expected_network_path.tunnel_interface
    cidr = request.tunnel_cidrs[0] if request.tunnel_cidrs else None
    if interface and interface.startswith("wg"):
        return {"kind": "wireguard", "name": interface, "cidr": cidr}
    if not interface and cidr:
        return {"kind": "wireguard", "name": request.source_db_host.split(".")[0], "cidr": cidr}
    return {"kind": "other", "name": interface or "", "cidr": cidr}


def _network(
    request: NetworkPreflightRequest,
    *,
    source_addr: str,
    node: str | None,
    namespace: str,
    detail: str,
) -> dict:
    return {
        "source_cidr": ",".join(request.aws_vpc_cidrs),
        "target_cidr": ",".join(request.onprem_routed_cidrs),
        "path": _path(request),
        "source_db": request.database,
        "source_addr": source_addr,
        "replication_node": node or "",
        "target_db": namespace,
        "target_db_detail": detail,
    }


CHECK_NAMES = ("check_cidr_overlap", "check_route", "probe_db_path", "probe_large_transfer")


def running_event_data(request: NetworkPreflightRequest, fingerprint: str) -> dict:
    """Before the probe: every check PENDING, no verdict yet, the same network block the result
    will carry (from the request and its probe target)."""
    target = request.probe_target
    return {
        "status": "RUNNING",
        "checks": [
            {"name": name, "status": "PENDING", "code": None, "evidence": ""}
            for name in CHECK_NAMES
        ],
        "probe_location": _probe_location(target.agent_id, target.namespace, target.node_name),
        "input_fingerprint": fingerprint,
        "checked_at": None,
        "may_start": False,
        "network": _network(
            request,
            source_addr=f"{request.source_db_host}:{request.source_db_port}",
            node=target.node_name,
            namespace=target.namespace,
            detail=f"{target.agent_id} ({target.cluster_uid})",
        ),
    }


def emit_preflight_running(request: NetworkPreflightRequest, fingerprint: str, started_at: str):
    """Once per check run (the key carries the start time, so a later run shows RUNNING again;
    `preflight:running:` never matches a result key). Never raises."""
    return emit_event(
        request.migration_id,
        "data.preflight",
        PHASE,
        "running",
        running_event_data(request, fingerprint),
        idempotency_key=f"preflight:running:{fingerprint}:{started_at}",
    )


def emit_preflight(
    request: NetworkPreflightRequest, result: NetworkPreflightResult, may_start: bool
):
    """Never raises (ticket 24 E9): a rejected/unstored event only leaves an operational log."""
    return emit_event(
        request.migration_id,
        "data.preflight",
        PHASE,
        "done" if result.status == "PASS" else "failed",
        preflight_event_data(request, result, may_start),
        idempotency_key=f"preflight:{result.input_fingerprint}:{checked_at_key(result)}",
    )


def checked_at_key(result: NetworkPreflightResult) -> str:
    return result.checked_at.isoformat()
