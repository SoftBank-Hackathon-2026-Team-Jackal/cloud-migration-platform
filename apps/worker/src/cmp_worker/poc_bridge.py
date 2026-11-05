"""PoC bridge: Finals `poc/demo` panel values → migration events (ticket 24, until Data/Traffic/
Cutover run on the platform).

Sources (Softbank-Hackathon-2026-Finals, read-only):
- panel `GET /api/state` (1 s sampler): `replication{phase, lag, tables[]}`, `readiness{...}`
  (same rule as `poc/cdc/scripts/readiness.py`) → `data.phase`, `data.lsn`, `data.table`,
  `data.readiness`
- `poc/cdc/results/switchover-*.json` (written by `05_switchover_fast.py` on success) →
  `cutover.step` per step, `cutover.outcome` (`steps_ms` → `steps`), `cutover.result` checksum, in
  the PoC fast order: sequence pre-bump before PAUSE, subscription off/on in parallel
- a failed switchover (exit code, stderr) → `cutover.outcome` refused + masked operational log

    uv run python -m cmp_worker.poc_bridge watch --migration-id M --panel http://127.0.0.1:8090 \
        --results-dir ../Softbank-Hackathon-2026-Finals/poc/cdc/results
    ... 05_switchover_fast.py ... 2> err.txt || uv run python -m cmp_worker.poc_bridge \
        switchover-failed --migration-id M --rc $? < err.txt

Values only: the panel's Korean `stage`/`reasons` text is never forwarded.
"""

import argparse
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---- event drafts ----


@dataclass(frozen=True)
class Draft:
    type: str
    phase: str
    status: str
    data: dict
    key: str
    progress: float | None = None


# panel replication.phase (raw) → web DataPhase; `/api/metrics` sends the Korean label instead.
PHASES = {"schema": "schema", "copying": "copy", "catching_up": "catchup", "streaming": "streaming"}
PHASES_KO = {
    "스키마만 복사됨": "schema",
    "초기 복사 중": "copy",
    "변경 따라잡는 중": "catchup",
    "실시간 동기화": "streaming",
}
# pg_subscription_rel.srsubstate → web table state (i init, d data copy, f/s copied, r ready)
TABLE_STATES = {"i": "waiting", "d": "copying", "f": "copied", "s": "copied", "r": "copied"}


def state_events(state: dict) -> list[Draft]:
    """One panel sample (`/api/state` or `/api/metrics`) → data.* drafts."""
    out: list[Draft] = []
    stamp = str(state.get("t") or state.get("time") or "")
    rep = state.get("replication") or {}
    phase = PHASES.get(rep.get("phase")) or PHASES_KO.get(rep.get("phase"))
    if phase:
        out.append(Draft("data.phase", "data_copy", "running", {"phase": phase}, f"phase:{phase}"))
    lag = rep.get("lag", rep.get("lag_bytes"))
    if isinstance(lag, int | float) and lag >= 0:
        out.append(
            Draft(
                "data.lsn",
                "data_copy",
                "running",
                {
                    # The panel reports only the slot lag; LSN text is not sampled yet.
                    "source_lsn": str(rep.get("source_lsn") or ""),
                    "target_lsn": rep.get("target_lsn"),
                    "lag_bytes": int(lag),
                    "lag_ms": None,
                },
                f"lsn:{stamp}",
            )
        )
    for table in rep.get("tables") or []:
        copied = table.get("copying_rows")
        out.append(
            Draft(
                "data.table",
                "data_copy",
                "running",
                {
                    "name": str(table.get("name")),
                    "rows_copied": int(copied or 0),
                    "rows_total": 0,  # the PoC samples table bytes, not row counts
                    "state": TABLE_STATES.get(table.get("state") or "", "waiting"),
                },
                f"table:{table.get('name')}:{table.get('state')}:{copied}",
            )
        )
    ready = state.get("readiness") or state.get("switchover_readiness")
    if ready and ready.get("commits_per_sec") is not None:
        out.append(
            Draft(
                "data.readiness",
                "data_copy",
                "running",
                {
                    "longest_xact_ms": ready.get("longest_xact_ms", 0),
                    "lock_waiters": ready.get("lock_waiters", 0),
                    "commits_per_sec": ready["commits_per_sec"],
                    "ready": bool(ready.get("ready", ready.get("ready_now"))),
                },
                f"ready:{stamp}",
            )
        )
    return out


# 05_switchover_fast.py `steps_ms` key (suffix " (PAUSE 전)" etc. stripped) → web step ids, in
# execution order. `window`: pre = before PAUSE, pause = inside the write pause, None = after RESUME.
# `switch_subscriptions` turns the forward subscription off and the reverse one on together, so it
# becomes the parallel web steps `sub_off` + `reverse` (same start). The PoC times retarget and
# RESUME as one step (`retarget`), and has no separate liveness probe.
FAST_STEPS: list[tuple[str, tuple[str, ...], str | None]] = [
    ("prepare_reverse", ("prepare_reverse",), "pre"),
    ("seq_prebump", ("seq_raise",), "pre"),
    ("sync_window_on", ("sync_window_on",), "pre"),
    ("wait_ready", ("wait_ready",), "pre"),
    ("pause_wait_inflight", ("pause",), "pause"),
    ("marker_wait", ("marker",), "pause"),
    ("snapshot_seq_check", ("snapshot_seq_check",), "pause"),
    ("switch_subscriptions", ("sub_off", "reverse"), "pause"),
    ("retarget_resume", ("retarget",), "pause"),
    ("drop_forward", ("drop_forward",), None),
    ("verify", ("verify",), None),
]
_SUFFIX = re.compile(r"\s*\(.*\)\s*$")


def fast_steps(steps_ms: dict) -> list[dict]:
    """`steps_ms` → web `CutoverStepPlan[]` with `start_ms` inside the pause window."""
    steps = {_SUFFIX.sub("", k): v for k, v in (steps_ms or {}).items()}
    plan: list[dict] = []
    cursor = 0.0
    for key, ids, window in FAST_STEPS:
        if key not in steps:
            continue
        ms = max(float(steps[key]), 0.0)
        for step_id in ids:
            item: dict = {"id": step_id, "ms": ms}
            if window == "pause":
                item["start_ms"] = cursor
            if window:
                item["window"] = window
            plan.append(item)
        if window == "pause":
            cursor += ms
    return plan


def switchover_events(result: dict) -> list[Draft]:
    """A successful switchover JSON (written only on success, at the end) → `cutover.step` per
    step in PoC order, `cutover.outcome` with the timings, and the checksum result."""
    plan = fast_steps(result.get("steps_ms") or {})
    run = f"{result.get('from')}->{result.get('to')}:{result.get('write_pause_ms')}"
    out: list[Draft] = []
    for n, step in enumerate(plan, start=1):
        out.append(
            Draft(
                "cutover.step",
                "verification",
                "running",
                {"n": n, "state": "done", "method": "fast", "of": len(plan), "result": "ok",
                 **{k: v for k, v in step.items() if k != "start_ms"}},
                f"{run}:step:{n}",
                progress=round(100 * n / len(plan), 1),
            )
        )  # fmt: skip
    verify_ok = result.get("verify_ok")
    out.append(
        Draft(
            "cutover.outcome",
            "verification",
            "running",
            {
                "result": "switched",
                "active": "target",
                "pause_ms": result.get("write_pause_ms"),
                "steps": plan,
                "replication": {"direction": "reverse", "lag_ms": None},
            },
            f"{run}:outcome",
        )
    )
    out.append(
        Draft(
            "cutover.result",
            "verification",
            "done" if verify_ok is not False else "failed",
            {
                "id": "checksum",
                "value": {True: "MATCH", False: "MISMATCH"}.get(verify_ok, "SKIPPED"),
                "label": "checksum",
                "ok": verify_ok is not False,
            },
            f"{run}:checksum",
        )
    )
    return out


_REASON = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")


def switchover_refused(rc: int, stderr: str) -> Draft:
    """A switchover that did not finish: the web `refused` outcome (the run RESUMEs on the source).
    The PoC gives only stderr; its masked tail is the `message`, an UPPER_SNAKE code the `reason`."""
    from cmp_api.migration_events import mask_value

    tail = mask_value(stderr[-2000:])
    data: dict = {
        "result": "refused",
        "message": tail,
        "active": "source",
        "pause_ms": None,
        "replication": {"direction": "forward", "lag_ms": None},
    }
    found = _REASON.findall(stderr)
    if found:
        data["reason"] = found[-1]
    return Draft(
        "cutover.outcome", "verification", "waiting", data, f"refused:{rc}:{time.time_ns()}"
    )


# ---- emission ----


@dataclass
class Bridge:
    migration_id: str
    emitter: Any  # cmp_api.migration_events.EventEmitter
    seen: dict[str, Any] = field(default_factory=dict)

    def _actor(self):
        from cmp_api.migration_events import Actor

        return Actor(source="worker", workflow_id="poc-bridge", activity="poc_bridge")

    def emit(self, drafts: list[Draft], *, dedupe_by_type: bool = False) -> int:
        """Emit drafts; with `dedupe_by_type`, skip values equal to the last one per type/id."""
        sent = 0
        for draft in drafts:
            slot = f"{draft.type}:{draft.data.get('name', '')}"
            if dedupe_by_type and self.seen.get(slot) == draft.data:
                continue
            event = self.emitter.emit(
                self.migration_id,
                draft.type,
                draft.phase,
                draft.status,
                draft.data,
                progress=draft.progress,
                idempotency_key=draft.key,
                actor=self._actor(),
            )
            if event is not None:
                self.seen[slot] = draft.data
                sent += 1
        return sent

    def sample(self, state: dict) -> int:
        return self.emit(state_events(state), dedupe_by_type=True)

    def results(self, directory: Path) -> int:
        sent = 0
        for path in sorted(directory.glob("switchover-*.json")):
            if path.name in self.seen:
                continue
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            self.seen[path.name] = True
            sent += self.emit(switchover_events(result))
        return sent

    def failed(self, rc: int, stderr: str) -> None:
        self.emit([switchover_refused(rc, stderr)])
        self.emitter.log(
            self.migration_id,
            "error",
            "SWITCHOVER_FAILED",
            {"rc": rc, "stderr_tail": stderr[-2000:]},  # masked by the emitter
            actor=self._actor(),
        )


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def main(argv: list[str] | None = None) -> None:
    import sys

    from cmp_api.db import SessionLocal
    from cmp_api.migration_events import DbEventEmitter

    parser = argparse.ArgumentParser(description="PoC panel → migration events")
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch")
    watch.add_argument("--migration-id", required=True)
    watch.add_argument("--panel", default="http://127.0.0.1:8090")
    watch.add_argument("--results-dir", type=Path)
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--once", action="store_true")
    failed = sub.add_parser("switchover-failed")
    failed.add_argument("--migration-id", required=True)
    failed.add_argument("--rc", type=int, required=True)
    args = parser.parse_args(argv)

    bridge = Bridge(args.migration_id, DbEventEmitter(SessionLocal))
    if args.command == "switchover-failed":
        bridge.failed(args.rc, sys.stdin.read())
        return
    while True:
        try:
            bridge.sample(_fetch(args.panel.rstrip("/") + "/api/state"))
        except (OSError, ValueError):
            pass  # panel restarting; next tick retries
        if args.results_dir:
            bridge.results(args.results_dir)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
