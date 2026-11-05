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
    path = request.expected_network_path
    interface = path.tunnel_interface
    return {
        "status": result.status,
        "checks": checks,
        "probe_location": f"{location.agent_id}/{location.namespace}"
        + (f"@{location.node}" if location.node else ""),
        "input_fingerprint": result.input_fingerprint,
        "checked_at": checked_at,
        "may_start": may_start,
        "network": {
            "source_cidr": ",".join(request.aws_vpc_cidrs),
            "target_cidr": ",".join(request.onprem_routed_cidrs),
            "path": {
                "kind": "wireguard" if interface and interface.startswith("wg") else "other",
                "name": interface or "",
                "cidr": request.tunnel_cidrs[0] if request.tunnel_cidrs else None,
            },
            "source_db": request.database,
            "source_addr": result.tested_endpoint,
            "replication_node": location.node or "",
            "target_db": location.namespace,
            "target_db_detail": f"{location.agent_id} ({location.cluster_uid})",
        },
    }


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
