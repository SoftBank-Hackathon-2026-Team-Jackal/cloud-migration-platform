"""DB 전환 계획·실행 결과 → 웹 이벤트 `data` 값(`cutover.*`, `approval.required`, `data.verify`).

웹(cloud-migration-web)은 두 번째 승인 화면에서 `cutover.plan`을 읽고, 승인하면
`POST /migrations/{id}/approve?gate=cutover`를 보낸다. 그 뒤 `cutover.ready` → `cutover.step`(단계 상태) →
`cutover.outcome`(통과면 단계별 ms) → `cutover.verify`(테이블별) 순서로 받는다.

여기 함수는 값만 만든다(입출력 없음). Activity가 `Draft`를 그대로 `emit_event_async`로 넘긴다.
값과 코드만 담고 화면 문구와 비밀값은 담지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import DataMigrationRequest, DbEndpoint, ReadinessPolicy
from .cutover import CutoverSettings, SourceApp
from .sql import INTERNAL_TABLES as INTERNAL_QUALIFIED

METHOD = "fast"
# 실습에서 잰 쓰기 중단 시간(infra/data-migration/crosscloud-lab/README.md): k3d 4~7ms, EKS 안 21ms,
# EKS ↔ GKE WireGuard 110ms. 계획 화면에는 가장 긴 값을 보인다.
REHEARSAL = {
    "source": "infra/data-migration (k3d, EKS, EKS-GKE WireGuard)",
    "runs": 3,
    "methods": [{"method": METHOD, "pause_ms": 110}],
}
# 실행 전 계획에 보이는 단계(실제 시간은 cutover.outcome.steps로 온다).
PLAN_STEPS = [
    ("reverse_prep", "pre"),
    ("seq_raise", "pre"),
    ("sync_on", "pre"),
    ("wait_ready", "pre"),
    ("pause", "pause"),
    ("marker", "pause"),
    ("seq", "pause"),
    ("sub_off", "pause"),
    ("reverse", "pause"),
    ("liveness", "pause"),
    ("retarget", "pause"),
    ("resume", "pause"),
]
# 전환 도구가 만든 내부 테이블. 사용자 테이블 비교(cutover.verify·data.verify rows)에는 보이지 않는다.
# 실행기는 public 표 이름만 쓰므로 schema 없는 이름도 넣는다(정의는 sql.INTERNAL_TABLES 한 곳).
INTERNAL_TABLES = set(INTERNAL_QUALIFIED) | {t.rpartition(".")[2] for t in INTERNAL_QUALIFIED}
CUTOVER_APPROVAL = {"excluded": [], "rollback_policy": "reverse_replication", "gate": "cutover"}


@dataclass(frozen=True)
class Draft:
    type: str
    status: str
    data: dict[str, Any]
    key: str
    progress: float | None = None


def _where(db: DbEndpoint) -> str:
    if db.access == "AWS_EKS":
        return f"EKS {db.cluster_name} ({db.region})"
    if db.access == "GCP_GKE":
        return f"GKE {db.cluster_name} ({db.location})"
    return f"kubeconfig {db.kubeconfig_ref}"


def preconditions(sample: dict, policy: ReadinessPolicy) -> list[dict[str, Any]]:
    """승인 전에 확인할 조건. 트랜잭션·잠금은 전환 직전에 실행기가 다시 기다리므로(cutover.ready) 넣지 않는다."""
    lag = sample.get("lag_bytes")
    streaming = sample.get("phase") == "streaming" and not sample.get("stale")
    lag_ok = streaming and lag is not None and lag <= policy.lag_ok_bytes
    return [
        {
            "id": "lag",
            "state": "pass" if lag_ok else "wait",
            "value": "-" if lag is None else f"{lag} B",
            "evidence": f"phase {sample.get('phase', '-')} · lag <= {policy.lag_ok_bytes} B",
        },
        {
            "id": "pooler",
            "state": "pass",
            "value": "PgBouncer",
            "evidence": "app -> cmp-pgbouncer:6432 (PAUSE/RESUME)",
        },
        {
            "id": "reverse_sub",
            "state": "pass",
            "value": "at switch",
            "evidence": "reverse subscription created disabled, enabled in PAUSE (origin = none)",
        },
    ]


def plan_data(
    req: DataMigrationRequest,
    app: SourceApp,
    settings: CutoverSettings,
    *,
    sample: dict,
    readiness: dict,
    last_steps: list[dict] | None = None,
    shadow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """두 번째 승인 화면의 `cutover.plan`. 지난 전환이 있으면 그 단계 시간을 보인다.
    `shadow`(Provisioning 84 그림자 리허설 줄)는 표시만 한다: `plan_ready` 판정에 넣지 않는다."""
    steps = last_steps or [{"id": i, "ms": 0, "window": w} for i, w in PLAN_STEPS]
    return {
        "method": METHOD,
        "expected_pause_ms": {
            "min": settings.expected_pause_min_ms,
            "max": settings.expected_pause_max_ms,
        },
        "rehearsal": REHEARSAL,
        "preconditions": preconditions(sample, req.policy) + ([shadow] if shadow else []),
        "steps": steps,
        "rollback": {"standby_hours": settings.standby_hours},
        "topology": {
            "app": f"{app.namespace}/{app.deployment}",
            "pooler": "PgBouncer",
            "source_db": f"{req.source.namespace}/{req.source.pod}",
            "target_db": f"{req.target.namespace}/{req.target.pod}",
            "source_where": _where(req.source),
            "target_where": _where(req.target),
            "writes_per_sec": max(float(readiness.get("commits_per_sec") or 0), 0),
        },
        "handover": [],  # DNS·원본 보존·접근 권한 인계는 Traffic·운영 쪽이 정한다
    }


def plan_ready(sample: dict, policy: ReadinessPolicy) -> bool:
    return all(item["state"] == "pass" for item in preconditions(sample, policy))


def ready_data(
    attempt: int, ready: dict | None, policy: ReadinessPolicy, commits_per_sec: float
) -> dict:
    """실행기가 PAUSE 직전에 본 마지막 준비도 표본 → `cutover.ready`."""
    ready = ready or {}
    reasons = ready.get("reasons") or []
    data = {
        "attempt": attempt,
        "longest_tx_ms": max(float(ready.get("longest_xact_ms") or 0), 0),
        "lock_waits": max(int(ready.get("lock_waiters") or 0), 0),
        "commits_per_sec": max(float(commits_per_sec or 0), 0),
        "ready": bool(ready.get("ready")),
        "limits": {"longest_tx_ms": policy.max_xact_ms, "lock_waits": 0},
    }
    if reasons:
        data["waiting_on"] = ",".join(reasons)
    if ready.get("waited_ms") is not None:
        data["waited_ms"] = max(float(ready["waited_ms"]), 0)
    return data


def run_events(
    result: dict, *, attempt: int, came_from: str, going_to: str, policy: ReadinessPolicy,
    commits_per_sec: float = 0,
) -> list[Draft]:  # fmt: skip
    """실행기 결과 한 번 → cutover.ready, cutover.step(단계마다), cutover.outcome, 통과면 검증 이벤트.

    came_from·going_to는 "source"|"target"(전환이면 source → target, 롤백이면 반대)."""
    key = f"{attempt}:{came_from}"
    out = [Draft("cutover.ready", "running",
                 ready_data(attempt, result.get("ready"), policy, commits_per_sec),
                 f"{key}:ready")]  # fmt: skip
    switched = bool(result.get("switched"))
    steps = [dict(step) for step in result.get("steps") or []]
    marks = [{**step, "result": "ok"} for step in steps]
    if not switched:
        if marks:
            marks[-1]["result"] = "fail"
        if result.get("paused"):
            marks.append({"id": "resume_source", "ms": 0, "window": "pause", "result": "revert"})
    total = len(marks)
    for n, step in enumerate(marks, start=1):
        data = {"n": n, "state": "done", "id": step["id"], "method": METHOD, "of": total,
                "result": step["result"], "window": step["window"]}  # fmt: skip
        out.append(Draft("cutover.step", "running", data, f"{key}:step:{n}",
                         progress=round(100 * n / total, 1)))  # fmt: skip
    # 역방향 복제: source → target 전환이면 대상 → 원본(reverse), 롤백이면 다시 원본 → 대상(forward)
    live = "reverse" if (going_to if switched else came_from) == "target" else "forward"
    if switched:
        outcome = {"result": "switched", "active": going_to,
                   "pause_ms": result.get("write_pause_ms"), "steps": steps,
                   "replication": {"direction": live, "lag_ms": None}}  # fmt: skip
        out.append(Draft("cutover.outcome", "running", outcome, f"{key}:outcome"))
    else:
        code = str(result.get("code") or "SWITCHOVER_FAILED")
        outcome = {"result": "refused", "reason": code, "message": result.get("detail") or code,
                   "active": came_from, "pause_ms": None,
                   "replication": {"direction": live, "lag_ms": None}}  # fmt: skip
        out.append(Draft("cutover.outcome", "waiting", outcome, f"{key}:outcome"))
        return out
    out.extend(verify_events(result, key=key, n=max(total, 1)))
    return out


def verify_events(result: dict, *, key: str, n: int) -> list[Draft]:
    """RESUME 뒤 스냅샷 비교 → cutover.step(verify), 테이블마다 cutover.verify, 합계 data.verify."""
    tables = [row for row in result.get("tables") or [] if row["table"] not in INTERNAL_TABLES]
    verify_ok = result.get("verify_ok")
    out = [Draft("cutover.step", "running", {"n": min(n, 32), "state": "verify", "of": min(n, 32)},
                 f"{key}:verify")]  # fmt: skip
    if verify_ok is None:  # verify = none: 비교하지 않았다
        return out
    for row in tables:
        data = {"table": row["table"], "source_rows": row["source_rows"],
                "target_rows": row["target_rows"],
                "checksum": "match" if row["match"] else "mismatch"}  # fmt: skip
        out.append(Draft("cutover.verify", "running", data, f"{key}:table:{row['table']}"))
    # 합계는 웹 script.ts DATA_VERIFY_TIMELINE 순서: rows running → rows 결과 → checksum running →
    # checksum 결과. 이벤트 status는 마지막(checksum done)만 done, 실패면 failed, 나머지는 running.
    rows = "done" if all(r["source_rows"] == r["target_rows"] for r in tables) else "failed"
    source = sum(row["source_rows"] for row in tables)
    target = sum(row["target_rows"] or 0 for row in tables)
    state = "done" if verify_ok else "failed"
    out.extend([
        Draft("data.verify", "running", {"name": "rows", "state": "running"}, f"{key}:rows:running"),
        Draft("data.verify", "running" if rows == "done" else "failed",
              {"name": "rows", "state": rows, "source": source, "target": target}, f"{key}:rows"),
        Draft("data.verify", "running", {"name": "checksum", "state": "running"},
              f"{key}:checksum:running"),
        Draft("data.verify", state, {"name": "checksum", "state": state}, f"{key}:checksum"),
    ])  # fmt: skip
    return out
