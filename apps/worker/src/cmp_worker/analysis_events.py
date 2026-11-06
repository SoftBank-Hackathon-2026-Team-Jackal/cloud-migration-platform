"""What the analysis Activities report to the web screen (ticket 58).

Each scan Activity, after it succeeds, turns its result into `analysis.finding` events: one per
key that has a value (`(key, count, detail)` items below). The web column shows one row per key
and each stream's bar follows the `progress` of its last finding, so progress stays below 100
until `check_evidence` sends the end markers (`code-done`, `infrastructure-done`, both 100) and
then `analysis.done` once. `message` is empty (events carry values, not text) and `detail` holds
the count and, where useful, one non-secret identifier (commit, cluster name/version, VPC CIDR,
driver module, probe path, resource kinds): never `.env` or Secret values, never env names.

Retries: the idempotency key `ana:{activity}:{key}` keeps one event per key within an attempt; a
Temporal retry is a new attempt (E7), and the web upserts findings by `id`, so a repeated finding
replaces the earlier row. Emitting never fails the analysis.
"""

import asyncio
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from cmp_scanners.aws_infra import AwsInfraScan
from cmp_scanners.code import CodeScan
from cmp_scanners.manifests import ManifestScan
from temporalio import activity

from cmp_worker.events import emit_event_async

Stream = Literal["code", "infrastructure"]
Item = tuple[str, int, str]  # key, count, detail

STREAM: dict[str, Stream] = {
    **dict.fromkeys(
        (
            "commit", "files", "manifests", "services", "statefulsets",
            "db_driver", "env_keys", "health_endpoint", "resources",
        ),
        "code",
    ),
    **dict.fromkeys(
        (
            "eks_cluster", "node", "vpc", "subnet", "security_group",
            "alb", "iam_role", "nat_gateway", "ebs_gb", "trail_calls",
        ),
        "infrastructure",
    ),
}  # fmt: skip
VIA: dict[str, str] = {
    "commit": "code", "files": "code", "db_driver": "code", "env_keys": "code",
    "manifests": "config", "services": "config", "statefulsets": "config",
    "health_endpoint": "config", "resources": "config",
    "eks_cluster": "config", "node": "config", "vpc": "config", "subnet": "config",
    "security_group": "config", "alb": "config", "nat_gateway": "config", "ebs_gb": "config",
    "iam_role": "iam", "trail_calls": "trail",
}  # fmt: skip
# Progress band (start, end) per Activity within its stream, in the web mock order.
BAND: dict[str, tuple[int, int]] = {
    "fetch_source": (10, 20),
    "scan_aws_infra": (15, 90),
    "scan_manifests": (30, 50),
    "scan_code": (55, 75),
    "build_requirements": (85, 90),
}
MARKERS: tuple[tuple[str, Stream], ...] = (
    ("infrastructure-done", "infrastructure"),
    ("code-done", "code"),
)


def _present(items: Iterable[Item]) -> list[Item]:
    return [item for item in items if item[1] > 0]


def _count_files(workspace: str) -> int:
    total = 0
    for _, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d != ".git"]
        total += len(files)
    return total


def source_findings(source: dict) -> list[Item]:
    commit = source.get("commit") or ""
    workspace = source.get("workspace")
    files = _count_files(workspace) if workspace and Path(workspace).is_dir() else 0
    return _present([("commit", 1 if commit else 0, commit[:12]), ("files", files, "")])


def manifest_findings(scan: ManifestScan) -> list[Item]:
    evidence = [
        *(w.evidence for w in scan.workloads),
        *(d.evidence for d in scan.cloud_dependencies),
        *(p.evidence for p in scan.postgres_images),
        *(u.evidence for u in scan.unconvertible),
        *(s.evidence for s in scan.secrets),
        *(v.evidence for v in scan.volume_claims),
        *(c.evidence for c in scan.configmaps),
        *(r.evidence for r in scan.network_rules),
    ]
    files = {e.split("#", 1)[0] for e in evidence} | {u.path for u in scan.unparsed}
    paths = sorted(
        {
            probe.split(":")[1]
            for w in scan.workloads
            for c in w.containers
            for probe in (c.readiness_probe, c.liveness_probe)
            if probe and probe.startswith("http:")
        }
    )
    kinds = [w.kind for w in scan.workloads]
    return _present(
        [
            ("manifests", len(files), ""),
            ("services", kinds.count("Deployment"), ""),
            ("statefulsets", kinds.count("StatefulSet"), ""),
            ("health_endpoint", len(paths), ", ".join(paths[:3])),
        ]
    )


def code_findings(scan: CodeScan) -> list[Item]:
    drivers = sorted({s.target for s in scan.call_sites if s.kind == "DB_DRIVER"})
    env_reads = {s.target for s in scan.call_sites if s.kind == "ENV_READ"}
    # env_keys: the count only; names stay in the artifact store.
    return _present(
        [("db_driver", len(drivers), ", ".join(drivers[:3])), ("env_keys", len(env_reads), "")]
    )


def requirements_findings(requirements: dict | None) -> list[Item]:
    resources = (requirements or {}).get("resources") or []
    kinds: dict[str, int] = {}
    for resource in resources:
        kinds[resource["kind"]] = kinds.get(resource["kind"], 0) + 1
    detail = " · ".join(f"{kind} {n}" for kind, n in kinds.items())
    return _present([("resources", len(resources), detail)])


def infra_findings(scan: AwsInfraScan) -> list[Item]:
    # No node, NAT gateway or EBS data in the read-only scan: those keys are never sent.
    cluster = scan.cluster
    cidrs = [cidr for vpc in scan.vpcs for cidr in vpc.cidrs]
    return _present(
        [
            ("eks_cluster", 1, " ".join(p for p in (cluster.name, cluster.version) if p)),
            ("vpc", len(scan.vpcs), ", ".join(cidrs[:3])),
            ("subnet", len(scan.subnets), ""),
            ("security_group", len(scan.security_groups), ""),
            ("alb", sum(1 for lb in scan.load_balancers if lb.type == "application"), ""),
            ("iam_role", len(scan.role_policies), ""),
            ("trail_calls", sum(e.count for e in scan.cloudtrail), ""),
        ]
    )


def _progress(band: tuple[int, int], index: int, total: int) -> int:
    start, end = band
    if total <= 1:
        return end
    return round(start + (end - start) * index / (total - 1))


async def emit_findings(migration_id: str, step: str, items: list[Item]) -> None:
    """One `analysis.finding` per item, progress rising inside the Activity's band."""
    for index, (key, count, detail) in enumerate(items):
        progress = _progress(BAND[step], index, len(items))
        await emit_event_async(
            migration_id,
            "analysis.finding",
            "analysis",
            "running",
            {
                "id": f"{STREAM[key]}-{key}",
                "stream": STREAM[key],
                "key": key,
                "via": [VIA[key]],
                "progress": progress,
                "message": "",  # no text in events: the web picks its words by `key`
                "detail": f"{count} · {detail}" if detail else str(count),
            },
            progress=progress,
            idempotency_key=f"ana:{step}:{key}",
        )


async def emit_done(migration_id: str) -> None:
    """Both streams to 100 (end markers the web leaves out of its columns), then done once."""
    for key, stream in MARKERS:
        await emit_event_async(
            migration_id,
            "analysis.finding",
            "analysis",
            "running",
            {
                "id": key,
                "stream": stream,
                "key": key,
                "via": ["config" if stream == "infrastructure" else "code"],
                "progress": 100,
                "message": "",
                "detail": "",
            },
            progress=100,
            idempotency_key=f"ana:check_evidence:{key}",
        )
    await emit_event_async(
        migration_id,
        "analysis.done",
        "analysis",
        "done",
        {},
        progress=100,
        idempotency_key="ana:check_evidence:done",
    )


async def report(migration_id: str, step: str, build: Callable[[], list[Item]]) -> None:
    """Build the items (store reads run in a thread) and emit them; a reporting error is logged
    and never fails the step."""
    try:
        await emit_findings(migration_id, step, await asyncio.to_thread(build))
    except Exception as exc:  # noqa: BLE001 - observation must not fail the analysis (E9)
        activity.logger.warning("analysis findings not emitted: %s %s", step, type(exc).__name__)
