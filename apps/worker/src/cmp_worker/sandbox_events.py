"""Sandbox loop events for the web rehearsal screen (ticket Provisioning 59).

Every event goes to `phase: build`. The loop's Activities emit their own step events when the
Workflow hands them an `events` context `{migration_id, run, attempt}`; Workflow-level events
(`sandbox.attempt`, `sandbox.done`, `build.review`) go through the `sbx_emit` Activity. Without the
context nothing is emitted, so histories from before this ticket replay and old payloads behave as
before.

`run` is the screen's track number: one per `sbx_apply`. `attempt` is the loop's fix attempt
(1..3); an INFRA retry starts a new run with the same attempt. Keys follow
`sbx:{run}:{attempt}:{step}:{state}` and are kept across Temporal retries of the same Activity.

Values and codes only: no secret values, no `.env` values, no response bodies.
"""

import difflib
from pathlib import Path
from typing import Any

from cmp_scanners.masking import mask_text
from temporalio import activity

MAX_ATTEMPTS = 3
PHASE = "build"
BUILD_ERROR_CODES = frozenset(
    {"SOURCE_NOT_FOUND", "DOCKERFILE_MISSING", "BUILD_FAILED", "PUSH_FAILED", "TIMEOUT"}
)
NO_CHECKS = {
    "required_passed": 0,
    "required_total": 0,
    "unsupported_calls": 0,
    "calls_in_mapping": 0,
    "calls_total": 0,
    "blocked_egress": 0,
}
MAX_FIX_LINES = 12


def event_key(run: int, attempt: int, step: str, state: str) -> str:
    return f"sbx:{run}:{attempt}:{step}:{state}"


def context(migration_id: str, run: int, attempt: int) -> dict[str, Any]:
    return {"migration_id": migration_id, "run": run, "attempt": attempt}


async def emit(
    ctx: dict[str, Any] | None,
    event_type: str,
    data: dict[str, Any],
    *,
    step: str,
    state: str,
    status: str = "running",
) -> None:
    """Emit one event for the loop context; nothing when there is no context."""
    if not ctx:
        return
    from cmp_worker.events import emit_event_async  # Worker-side only (DB emitter)

    await emit_event_async(
        ctx["migration_id"],
        event_type,
        PHASE,
        status,
        data,
        idempotency_key=event_key(ctx["run"], ctx["attempt"], step, state),
        across_retries=True,
    )


class Steps:
    """`sandbox.step` running → done/failed for one run; `fail()` marks the current step."""

    def __init__(self, ctx: dict[str, Any] | None):
        self.ctx = ctx
        self.current: str | None = None

    async def start(self, step: str) -> None:
        self.current = step
        await self._send(step, "running")

    async def done(self) -> None:
        if self.current is not None:
            await self._send(self.current, "done")
        self.current = None

    async def fail(self) -> None:
        if self.current is not None:
            await self._send(self.current, "failed")
        self.current = None

    async def _send(self, step: str, state: str) -> None:
        if not self.ctx:
            return
        data = {
            "run": self.ctx["run"],
            "attempt": self.ctx["attempt"],
            "step": step,
            "state": state,
        }
        await emit(self.ctx, "sandbox.step", data, step=step, state=state)


# ---------------------------------------------------------------- data builders (pure)


def image_digest(images: dict[str, Any] | None) -> str | None:
    """Digest of the first application image (sorted by resource ID); the demo has one."""
    if not images:
        return None
    uri = images[min(images)]["uri"]
    return uri.partition("@")[2] or None


def build_result(
    attempt: int,
    *,
    images: dict[str, Any] | None,
    reused: bool = False,
    error_code: str | None = None,
) -> dict[str, Any]:
    if error_code is not None or not images:
        code = error_code if error_code in BUILD_ERROR_CODES else "BUILD_FAILED"
        return {
            "attempt": attempt, "max_attempts": MAX_ATTEMPTS, "status": "FAILED",
            "digest": None, "image_uri": None, "reused": False, "error_code": code,
        }  # fmt: skip
    uri = images[min(images)]["uri"]
    return {
        "attempt": attempt, "max_attempts": MAX_ATTEMPTS, "status": "SUCCEEDED",
        "digest": image_digest(images), "image_uri": uri, "reused": reused, "error_code": None,
    }  # fmt: skip


def tests_data(cases: list[Any]) -> dict[str, int]:
    """Counts of the pinned suite by origin; `read_only` counts tests that write nothing."""
    by_source = {"STANDARD": 0, "ORIGINAL": 0, "AI": 0}
    for case in cases:
        by_source[case.source] = by_source.get(case.source, 0) + 1
    return {
        "total": len(cases),
        "catalog": by_source["STANDARD"],
        "original": by_source["ORIGINAL"],
        "generated": by_source["AI"],
        "read_only": sum(1 for c in cases if c.effect == "READ_ONLY"),
    }


def checks_data(
    results: list[dict[str, Any]], egress: list[Any], reasons: list[str]
) -> dict[str, int]:
    """Judge signals for the screen: tests that ran, outside calls the proxy allowed or blocked."""
    ran = [r for r in results if r["status"] != "SKIPPED"]
    return {
        "required_passed": sum(1 for r in ran if r["status"] == "PASSED"),
        "required_total": len(ran),
        "unsupported_calls": sum(1 for r in reasons if r.startswith("UNSUPPORTED_CALL:")),
        "calls_in_mapping": sum(1 for e in egress if e.decision == "ALLOW"),
        "calls_total": len(egress),
        "blocked_egress": sum(1 for e in egress if e.decision == "BLOCK"),
    }


def attempt_data(
    run: int,
    attempt: int,
    *,
    verdict: dict[str, Any],
    failure_class: str | None,
    checks: dict[str, int] | None,
    fix_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "run": run, "attempt": attempt, "max_attempts": MAX_ATTEMPTS,
        "verdict": {"passed": bool(verdict["passed"]), "reasons": list(verdict["reasons"])},
        "failure_class": failure_class, "checks": dict(checks or NO_CHECKS),
        "fix_ids": list(fix_ids or []),
    }  # fmt: skip


def _is_env_file(path: str) -> bool:
    name = Path(path).name
    return name.startswith(".env") or name.endswith(".env")


def _shown(line: str, env_file: bool) -> str:
    """One diff line for the screen. A `.env` line keeps only its key; others are masked."""
    if env_file:
        key = line.split("=", 1)[0].strip()
        return key if key and not key.startswith("#") else ""
    return mask_text(line.rstrip("\n"))


def fix_lines(workspace: str | None, path: str, content: str) -> list[dict[str, str]]:
    """Removed and added lines of one fixed file (no context), at most `MAX_FIX_LINES`."""
    before = ""
    if workspace:
        old = Path(workspace) / path
        if old.is_file():
            before = old.read_text(encoding="utf-8", errors="replace")
    env_file = _is_env_file(path)
    lines: list[dict[str, str]] = []
    for raw in difflib.unified_diff(before.splitlines(), content.splitlines(), n=0, lineterm=""):
        if raw.startswith(("---", "+++", "@@")) or raw[:1] not in "+-":
            continue
        text = _shown(raw[1:], env_file)
        if not text:
            continue
        kind = "add" if raw[0] == "+" else "del"
        lines.append({"kind": kind, "text": ("+ " if kind == "add" else "- ") + text})
        if len(lines) >= MAX_FIX_LINES:
            break
    return lines


def requirements_fix_lines(fix: Any) -> list[dict[str, str]]:
    """What a requirements fix changes, as names and values from the requirements (no secrets)."""
    lines = [f"+ {e.resource_id}.{e.field}: {e.value}" for e in fix.edits]
    lines += [f"+ {r.row_id}: {r.disposition}" for r in fix.row_changes]
    for sign, intents in (("+", fix.add_intents), ("-", fix.remove_intents)):
        for i in intents:
            to = i.external_host or i.destination_resource_id
            lines.append(f"{sign} {i.source_resource_id} -> {to}:{i.port}")
    shown = [{"kind": "add" if t[0] == "+" else "del", "text": t} for t in lines]
    return shown[:MAX_FIX_LINES]


# ---------------------------------------------------------------- Workflow-level events


@activity.defn(name="sbx_emit")
async def sbx_emit(request: dict) -> int:
    """`{migration_id, events: [{type, status, data, key}]}` → number stored. For the Sandbox
    Workflow, which may not emit itself. A rejected event is logged by the emitter, not raised."""
    from cmp_worker.events import emit_event_async

    stored = 0
    for item in request["events"]:
        event = await emit_event_async(
            request["migration_id"],
            item["type"],
            PHASE,
            item["status"],
            item["data"],
            idempotency_key=item["key"],
            across_retries=True,
        )
        stored += event is not None
    return stored
