"""Web analysis screen events from the analysis Activities (issue #226; guide: events-guide.md §2).

One `analysis.scan` per finished scan, `analysis.done` once requirements are built. Pure
summaries of results the pipeline already stored: they never change what the analysis finds.

- `items` carry key names and counts only (web `analysis.item.<key>`), never values: no source
  text, `.env` values, Secret keys, tokens or account IDs.
- `completed`/`total` is a fixed slot per scan inside its stream (the web keeps the largest), so
  scans that run in parallel or retry still report a deterministic n/m.
- The event `migration_id` is the analysis input's `migration_id`; `MigrationWorkflow` sets it to
  its own Workflow ID (`migration-<uuid>`) before starting the child.
- Emitting never fails the Activity: a summary error leaves an operational log only.
"""

import asyncio
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cmp_scanners.aws_infra import AwsInfraScan
from cmp_scanners.code import CodeScan
from cmp_scanners.manifests import ManifestScan
from cmp_scanners.masking import mask_text

from cmp_worker.events import emit_event_async, log_event

# activity -> (stream, slot). Totals per stream follow from the slots.
SLOTS: dict[str, tuple[str, int]] = {
    "fetch_source": ("code", 1),
    "scan_manifests": ("code", 2),
    "scan_code": ("code", 3),
    "scan_aws_infra": ("infrastructure", 1),
    "scan_network": ("infrastructure", 2),  # built inside `build_requirements` (build_network)
}
TOTALS = {
    stream: max(slot for s, slot in SLOTS.values() if s == stream)
    for stream in ("code", "infrastructure")
}
MAX_EVIDENCE = 5
_SKIPPED_DIRS = {".git", "node_modules", "vendor", ".venv", "__pycache__"}
_UNVERIFIED = {"CLOUD_RESOURCE", "IAM_POLICY", "CLOUDTRAIL"}


def _item(key: str, n: float, sku: str | None = None) -> dict[str, Any]:
    return {"key": key, "n": n, **({"sku": sku} if sku else {})}


def _evidence(kind: str, locators: Iterable[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for locator in locators:
        if locator and len(out) < MAX_EVIDENCE:
            out.append(
                {"kind": kind, "locator": mask_text(locator), "verified": kind not in _UNVERIFIED}
            )
    return out


def repository_locator(repository: str, commit: str) -> str:
    """`host/path@sha7` without scheme, user info (tokens) or a `.git` suffix."""
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", repository.strip(), flags=re.IGNORECASE)
    value = value.rsplit("@", 1)[-1] if "@" in value.split("/", 1)[0] else value
    value = value.removesuffix(".git").rstrip("/")
    if value.startswith("/"):  # a local test repository: only its last name
        value = Path(value).name
    return f"{value}@{commit[:7]}"


def count_files(workspace: str | Path) -> int:
    total = 0
    for _, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIPPED_DIRS]
        total += len(files)
    return total


def summarize_source(repository: str, source: dict[str, Any]) -> tuple[list, list]:
    items = [_item("files", count_files(source["workspace"])), _item("commit", 1)]
    return items, _evidence("CODE", [repository_locator(repository, source["commit"])])


def summarize_manifests(scan: ManifestScan) -> tuple[list, list]:
    kinds = [w.kind for w in scan.workloads]
    files = {w.evidence.split(":", 1)[0] for w in scan.workloads}
    probes = any(
        c.readiness_probe or c.liveness_probe for w in scan.workloads for c in w.containers
    )
    items = [
        _item("manifests", len(files)),
        _item("services", kinds.count("Service")),
        _item("statefulsets", kinds.count("StatefulSet")),
        *([_item("health_endpoint", 1)] if probes else []),
    ]
    return items, _evidence("MANIFEST", (w.evidence for w in scan.workloads))


def summarize_code(scan: CodeScan) -> tuple[list, list]:
    drivers = [s for s in scan.call_sites if s.kind == "DB_DRIVER"]
    s3 = [s for s in scan.call_sites if s.kind == "AWS_SDK" and "s3" in s.target.lower()]
    env_keys = {s.target for s in scan.call_sites if s.kind == "ENV_READ"}
    items = [
        *(
            [_item("db_driver", len({d.target for d in drivers}), drivers[0].target)]
            if drivers
            else []
        ),
        *([_item("s3_sdk", len(s3))] if s3 else []),
        _item("env_keys", len(env_keys)),
    ]
    sites = [*drivers, *s3, *(s for s in scan.call_sites if s.kind == "ENV_READ")]
    return items, _evidence("CODE", (f"{s.file}:{s.line}" for s in sites))


def summarize_aws(infra: dict[str, Any], scan: AwsInfraScan | None) -> tuple[list, list]:
    if scan is None:  # NOT_COLLECTED / FAILED: an empty batch still completes the slot
        return [], []
    albs = [lb for lb in scan.load_balancers if lb.type == "application"]
    roles = {r.role_arn for r in scan.role_policies}
    calls = {(t.event_source, t.event_name) for t in scan.cloudtrail}
    items = [
        _item("eks_cluster", 1),
        _item("alb", len(albs)),
        _item("iam_role", len(roles)),
        _item("trail_calls", len(calls)),
    ]
    resources = 1 + len(scan.load_balancers) + len(scan.vpcs) + len(scan.subnets)
    resources += len(scan.security_groups)
    evidence = [
        *_evidence("CLOUD_RESOURCE", [f"eks:cluster/{scan.cluster.name}"]),
        *_evidence("IAM_POLICY", (r.split(":")[-1] for r in sorted(roles))),
        *_evidence(
            "CLOUDTRAIL", [" · ".join(sorted(f"{s.split('.')[0]}:{n}" for s, n in calls)[:3])]
        ),
    ]
    return [_item("resources", resources), *items], evidence


def summarize_network(
    scan: AwsInfraScan | None, network: dict[str, Any] | None
) -> tuple[list, list]:
    items: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    if scan is not None:
        items = [
            _item("vpc", len(scan.vpcs)),
            _item("subnet", len(scan.subnets)),
            _item("security_group", len(scan.security_groups)),
        ]
        evidence = _evidence("CLOUD_RESOURCE", (f"ec2:vpc/{v.id}" for v in scan.vpcs))
    entries = (network or {}).get("external_entries") or []
    evidence += _evidence(
        "CONFIG", (f"{e['protocol']} {e['port']} -> {e['target_resource_id']}" for e in entries)
    )
    return items, evidence[:MAX_EVIDENCE]


def scan_data(activity: str, items: list, evidence: list) -> dict[str, Any]:
    stream, slot = SLOTS[activity]
    return {
        "id": f"scan-{activity}",
        "activity": activity,
        "stream": stream,
        "completed": slot,
        "total": TOTALS[stream],
        "items": items,
        "evidence": evidence,
    }


async def emit_scan(migration_id: str, activity: str, summarize, *args) -> None:
    """Summarize (in a thread) and emit one `analysis.scan`; never raises."""
    try:
        items, evidence = await asyncio.to_thread(summarize, *args)
        data = scan_data(activity, items, evidence)
        await emit_event_async(
            migration_id,
            "analysis.scan",
            "analysis",
            "running",
            data,
            progress=round(data["completed"] / data["total"] * 100),
            idempotency_key=data["id"],
        )
    except Exception as exc:  # noqa: BLE001 — observation only: the scan result stands (E9)
        await asyncio.to_thread(
            log_event,
            migration_id,
            "warning",
            "ANALYSIS_EVENT_FAILED",
            {"activity": activity, "error": type(exc).__name__},
        )


async def emit_done(migration_id: str) -> None:
    try:
        await emit_event_async(
            migration_id, "analysis.done", "analysis", "done", {}, progress=100,
            idempotency_key="analysis-done",
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 — observation only (E9)
        await asyncio.to_thread(
            log_event, migration_id, "warning", "ANALYSIS_EVENT_FAILED",
            {"activity": "build_requirements", "error": type(exc).__name__},
        )  # fmt: skip
