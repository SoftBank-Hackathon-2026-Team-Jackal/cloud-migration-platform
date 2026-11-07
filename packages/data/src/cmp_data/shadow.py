"""그림자 리허설(Provisioning 84): 설정·판정·이벤트 값. 순수 함수만 둔다(Workflow에서 부른다).

복제가 streaming이 된 뒤 DB 전환 승인 전에, 중계 Envoy로 들어온 GET 요청을 대상에도 복사해(83의
`shadow_*` Activity) 원본·대상 응답을 비교한다. 결과는 **경고와 표시만** 한다: 전환 승인·전환을 막지
않고, 아무것도 고치지 않는다. 판정 기준은 `ShadowSettings`(입력 `data_migration.shadow`)다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .contracts import Strict

NO_TRAFFIC = "SHADOW_NO_TRAFFIC"
UNAVAILABLE = "SHADOW_UNAVAILABLE"
STOPPED_REPLICATION = "SHADOW_STOPPED_REPLICATION"
DISABLED = "SHADOW_DISABLED"  # 웹 표시용 코드. 꺼져 있으면 Workflow는 이벤트를 내지 않는다

MAX_SAMPLES = 5
MAX_DIFFS = 5
MAX_VALUE_CHARS = 200
AI_CATEGORIES = ("noise", "data", "env", "code")

ShadowState = Literal["pass", "warn", "skipped", "stopped"]


class ShadowSettings(Strict):
    """`data_migration.shadow`. 없거나 `enabled=false`면 그림자 리허설을 하지 않는다(지금 동작)."""

    enabled: bool = False
    window_s: int = Field(default=180, ge=1, le=3600)
    min_requests: int = Field(default=50, ge=1)
    percent: int = Field(default=100, ge=1, le=100)
    rate_per_sec: int = Field(default=20, ge=1, le=1000)
    pass_ratio: float = Field(default=0.99, ge=0, le=1)
    allow_paths: list[str] = Field(default_factory=list)


def _count(summary: dict | None, key: str) -> int:
    value = (summary or {}).get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def judge(summary: dict | None, settings: ShadowSettings) -> tuple[ShadowState, str | None]:
    """수집 요약 → (상태, 이유 코드). 수집 실패(요약 없음)는 `skipped`(UNAVAILABLE)."""
    if summary is None:
        return "skipped", UNAVAILABLE
    compared = _count(summary, "compared")
    if compared < settings.min_requests:
        return "skipped", NO_TRAFFIC
    if _count(summary, "match") / compared >= settings.pass_ratio and not _count(
        summary, "target_5xx"
    ):
        return "pass", None
    return "warn", None


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "…"
    if isinstance(value, dict | list):
        text = str(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"
    return value


def samples_for_event(summary: dict | None) -> list[dict[str, Any]]:
    """불일치 표본 최대 5개(차이 최대 5개, 값은 짧게). 이벤트 크기 제한(32 KiB) 안에 둔다."""
    out = []
    for sample in ((summary or {}).get("samples") or [])[:MAX_SAMPLES]:
        if not isinstance(sample, dict):
            continue
        diff = [
            {
                "pointer": str(d.get("pointer", "")),
                "origin": _short(d.get("origin")),
                "target": _short(d.get("target")),
            }
            for d in (sample.get("diff") or [])[:MAX_DIFFS]
            if isinstance(d, dict)
        ]
        out.append({
            "method": str(sample.get("method") or "GET"),
            "path": _short(str(sample.get("path") or "")),
            "status_origin": sample.get("status_origin"),
            "status_target": sample.get("status_target"),
            "diff": diff,
        })  # fmt: skip
    return out


def _ratio(numerator: Any, denominator: Any) -> float | None:
    if not isinstance(numerator, int | float) or not isinstance(denominator, int | float):
        return None
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def progress_data(run_id: str, summary: dict, elapsed_s: float, window_s: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "compared": _count(summary, "compared"),
        "match": _count(summary, "match"),
        "mismatch": _count(summary, "mismatch"),
        "elapsed_s": max(round(elapsed_s, 1), 0),
        "window_s": window_s,
    }


def result_data(
    run_id: str,
    state: ShadowState,
    reason: str | None,
    summary: dict | None,
    ai: dict | None = None,
) -> dict[str, Any]:
    """`shadow.result`."""
    compared = _count(summary, "compared")
    summary = summary or {}
    return {
        "run_id": run_id,
        "state": state,
        "compared": compared,
        "match": _count(summary, "match"),
        "mismatch": _count(summary, "mismatch"),
        "match_ratio": _ratio(_count(summary, "match"), compared),
        "target_5xx": _count(summary, "target_5xx"),
        "p99_ratio": _ratio(summary.get("target_p99_ms"), summary.get("origin_p99_ms")),
        "reason": reason,
        "samples": samples_for_event(summary),
        "ai": ai,
    }


def precondition(result: dict[str, Any], settings: ShadowSettings) -> dict[str, Any]:
    """`cutover.plan.preconditions`의 `shadow` 줄. 경고 전용이라 전환 준비 판정(`plan_ready`)에 넣지 않는다."""
    state = {"pass": "pass", "warn": "warn"}.get(result["state"], "skip")
    if result["reason"]:
        evidence = result["reason"]
    else:
        evidence = f"match >= {settings.pass_ratio:g} · target 5xx {result['target_5xx']}"
    return {
        "id": "shadow",
        "state": state,
        "value": f"{result['match']}/{result['compared']}",
        "evidence": evidence,
    }


def valid_ai(value: Any) -> dict[str, Any] | None:
    """AI 설명 결과 확인. 모양이 틀리면 None(진행에는 영향 없음)."""
    if not isinstance(value, dict) or value.get("category") not in AI_CATEGORIES:
        return None
    summary, hypotheses = value.get("summary"), value.get("hypotheses")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(hypotheses, list):
        return None
    texts = [h for h in hypotheses if isinstance(h, str) and h.strip()][:3]
    return {"category": value["category"], "summary": summary[:500],
            "hypotheses": [h[:300] for h in texts]}  # fmt: skip
