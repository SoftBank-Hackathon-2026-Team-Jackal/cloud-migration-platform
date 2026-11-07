"""그림자 리허설(Provisioning 84): DataMigrationWorkflow 관찰 단계.

83의 `shadow_*` Activity(relay 큐)·이벤트 발행·AI 설명은 가짜다. 확인: pass/warn/skipped/복제 중단,
finally의 `shadow_stop`, 꺼져 있으면 지금과 같은 Activity 순서, patched 재생 호환.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from cmp_contracts.events import validate_data
from cmp_contracts.queues import MIGRATION_QUEUE, PROVISIONING_QUEUE, WORKFLOW_QUEUE
from cmp_data import shadow as sh
from cmp_worker.workflows import data_migration as dmw
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from .test_data_workflow import ARGS, Fakes

RELAY = "relay-test"
SHADOW = {"enabled": True, "window_s": 4, "min_requests": 50, "percent": 100,
          "rate_per_sec": 20, "pass_ratio": 0.99, "allow_paths": ["/api/"]}  # fmt: skip
SAMPLE = {"method": "GET", "path": "/api/messages?id=1", "status_origin": 200,
          "status_target": 200,
          "diff": [{"pointer": "/0/created_at", "origin": "2026-11-08T01:00:00Z",
                    "target": "2026-11-08T01:00:01Z"}]}  # fmt: skip


def summary(compared=60, match=60, target_5xx=0, samples=()):
    return {"run_id": "r", "requests": compared, "compared": compared, "match": match,
            "mismatch": compared - match, "skipped": 0, "target_5xx": target_5xx,
            "origin_p99_ms": 20.0, "target_p99_ms": 30.0, "noise_fields": [],
            "samples": list(samples)}  # fmt: skip


class ShadowFakes(Fakes):
    def __init__(self, *, start_ok=True, summaries=None, summary_fails=False, explain=None,
                 explain_fails=False, phases=("copy", "streaming"), **kw):  # fmt: skip
        super().__init__(phases=phases, **kw)
        self.start_ok, self.summary_fails = start_ok, summary_fails
        self.summaries = list(summaries or [summary()])
        self.explain, self.explain_fails = explain, explain_fails
        self.events: list[dict] = []
        self.announced: list[dict] = []
        self.shadow_calls: list[tuple[str, dict]] = []

    def activities(self):
        base = [a for a in super().activities() if a.__name__ != "announce"]

        @activity.defn(name="dm_announce_cutover")
        async def announce(payload: dict) -> dict:
            self.calls.append(f"dm_announce_cutover:{payload['approval']}")
            self.announced.append(payload)
            return {"ready": True}

        return [*base, announce]

    def relay_activities(self):
        @activity.defn(name="shadow_start")
        async def start(payload: dict) -> dict:
            self.shadow_calls.append(("shadow_start", payload))
            if not self.start_ok:
                return {"ok": False, "code": "SHADOW_UNAVAILABLE"}
            return {"ok": True, "code": None}

        @activity.defn(name="shadow_summary")
        async def summ(payload: dict) -> dict:
            self.shadow_calls.append(("shadow_summary", payload))
            if self.summary_fails:
                raise ApplicationError("down", type="SHADOW_UNAVAILABLE", non_retryable=True)
            return self.summaries.pop(0) if len(self.summaries) > 1 else self.summaries[0]

        @activity.defn(name="shadow_stop")
        async def stop(payload: dict) -> dict:
            self.shadow_calls.append(("shadow_stop", payload))
            return {"ok": True}

        return [start, summ, stop]

    def provisioning_activities(self):
        @activity.defn(name="emit_migration_event")
        async def emit(request: dict) -> int:
            validate_data(request["type"], request["data"])  # 계약에 맞아야 저장된다
            self.events.append(request)
            return len(self.events)

        @activity.defn(name="shadow_explain")
        async def explain(payload: dict) -> dict | None:
            self.shadow_calls.append(("shadow_explain", payload))
            if self.explain_fails:
                raise ApplicationError("llm", type="LLM_UNAVAILABLE", non_retryable=True)
            return self.explain

        return [emit, explain]

    def of(self, event_type):
        return [e["data"] for e in self.events if e["type"] == event_type]

    def shadow_names(self):
        return [name for name, _ in self.shadow_calls]


def shadow_args(**shadow):
    return {**ARGS, "relay_queue": RELAY, "shadow": {**SHADOW, **shadow}}


async def run(fakes, args, *, record=False):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DataMigrationWorkflow]),
        Worker(env.client, task_queue=MIGRATION_QUEUE, activities=fakes.activities()),
        Worker(env.client, task_queue=RELAY, activities=fakes.relay_activities()),
        Worker(
            env.client, task_queue=PROVISIONING_QUEUE, activities=fakes.provisioning_activities()
        ),  # fmt: skip
    ):
        handle = await env.client.start_workflow(
            DataMigrationWorkflow.run, args, id=f"data-{uuid.uuid4()}", task_queue=WORKFLOW_QUEUE
        )
        for _ in range(400):
            if any(c.startswith("dm_announce_cutover") for c in fakes.calls):
                break
            await asyncio.sleep(0.05)
        await handle.signal(DataMigrationWorkflow.finish)
        result = await handle.result()
        status = await handle.query(DataMigrationWorkflow.status)
        history = await handle.fetch_history()
    return result, status, history


def scheduled(history) -> list[str]:
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]


# ---- 순수 판정 ----


def test_judge_follows_the_contract_thresholds() -> None:
    settings = sh.ShadowSettings.model_validate(SHADOW)
    assert sh.judge(summary(60, 60), settings) == ("pass", None)
    assert sh.judge(summary(100, 99), settings) == ("pass", None)
    assert sh.judge(summary(100, 98), settings) == ("warn", None)
    assert sh.judge(summary(60, 60, target_5xx=1), settings) == ("warn", None)
    assert sh.judge(summary(10, 10), settings) == ("skipped", sh.NO_TRAFFIC)
    assert sh.judge(None, settings) == ("skipped", sh.UNAVAILABLE)


def test_result_and_precondition_fit_the_event_contract() -> None:
    settings = sh.ShadowSettings.model_validate(SHADOW)
    data = sh.result_data("r1", "warn", None, summary(1240, 1238, samples=[SAMPLE] * 9))
    assert len(data["samples"]) == 5 and data["match_ratio"] == round(1238 / 1240, 4)
    assert data["p99_ratio"] == 1.5
    validate_data("shadow.result", data)
    line = sh.precondition(data, settings)
    assert line == {"id": "shadow", "state": "warn", "value": "1238/1240",
                    "evidence": "match >= 0.99 · target 5xx 0"}  # fmt: skip
    skipped = sh.result_data("r1", "skipped", sh.NO_TRAFFIC, summary(3, 3))
    assert sh.precondition(skipped, settings)["state"] == "skip"
    assert sh.precondition(skipped, settings)["evidence"] == sh.NO_TRAFFIC


def test_settings_default_to_off() -> None:
    assert sh.ShadowSettings().model_dump() == {
        "enabled": False, "window_s": 180, "min_requests": 50, "percent": 100,
        "rate_per_sec": 20, "pass_ratio": 0.99, "allow_paths": []}  # fmt: skip


def test_valid_ai_rejects_wrong_shapes() -> None:
    good = {"category": "noise", "summary": "timestamps differ", "hypotheses": ["clock"]}
    assert sh.valid_ai(good) == good
    assert sh.valid_ai({**good, "category": "fix"}) is None
    assert sh.valid_ai({**good, "hypotheses": "x"}) is None
    assert sh.valid_ai(None) is None


# ---- Workflow ----


async def test_disabled_runs_exactly_as_before() -> None:
    base, off = ShadowFakes(), ShadowFakes()
    _, _, history_base = await run(base, ARGS)
    _, status, history_off = await run(off, shadow_args(enabled=False))
    assert scheduled(history_off) == scheduled(history_base)
    assert off.shadow_calls == [] and off.events == []
    assert "shadow" not in str(off.announced) and status["shadow"] is None


async def test_pass_observes_then_announces_with_a_shadow_line() -> None:
    fakes = ShadowFakes()
    result, status, history = await run(fakes, shadow_args())
    assert result["status"] == "done"
    names = fakes.shadow_names()
    assert names[0] == "shadow_start" and names[-1] == "shadow_stop"
    start = fakes.shadow_calls[0][1]
    assert start["migration_id"] == "migration-x" and start["allow_paths"] == ["/api/"]
    assert (start["percent"], start["rate_per_sec"]) == (100, 20) and start["run_id"]
    assert {p["run_id"] for _, p in fakes.shadow_calls} == {start["run_id"]}
    assert fakes.of("shadow.progress")
    [res] = fakes.of("shadow.result")
    assert (res["state"], res["reason"], res["ai"]) == ("pass", None, None)
    assert "shadow_explain" not in names  # 불일치가 없으면 AI를 부르지 않는다
    line = fakes.announced[0]["shadow"]
    assert line["id"] == "shadow" and line["state"] == "pass" and line["value"] == "60/60"
    order = scheduled(history)
    # 관찰은 streaming 뒤, 앱 연결·전환 계획 알림 전에 끝난다
    assert order.index("shadow_stop") < order.index("dm_route_app")
    assert order.index("shadow_stop") < order.index("dm_announce_cutover")
    assert status["shadow"]["state"] == "pass"
    for event in fakes.events:
        assert event["phase"] == "data_copy"


async def test_warn_with_mismatch_asks_for_an_explanation_and_still_proceeds() -> None:
    ai = {"category": "noise", "summary": "created_at differs by 1s",
          "hypotheses": ["timestamps are generated per request"]}  # fmt: skip
    fakes = ShadowFakes(summaries=[summary(100, 90, samples=[SAMPLE])], explain=ai)
    result, _, _ = await run(fakes, shadow_args())
    assert result["status"] == "done"
    [res] = fakes.of("shadow.result")
    assert res["state"] == "warn" and res["ai"] == ai and res["samples"][0]["path"]
    explain = dict(fakes.shadow_calls)["shadow_explain"]
    assert explain["samples"] and explain["migration_id"] == "migration-x"
    assert fakes.announced[0]["shadow"]["state"] == "warn"
    names = fakes.shadow_names()  # 관찰을 멈춘 뒤에 설명을 구한다
    assert names.index("shadow_stop") < names.index("shadow_explain")


async def test_failed_explanation_leaves_ai_null() -> None:
    fakes = ShadowFakes(summaries=[summary(100, 90, samples=[SAMPLE])], explain_fails=True)
    result, _, _ = await run(fakes, shadow_args())
    assert result["status"] == "done"
    assert fakes.of("shadow.result")[0]["ai"] is None


async def test_too_few_requests_is_skipped_no_traffic() -> None:
    fakes = ShadowFakes(summaries=[summary(5, 5)])
    await run(fakes, shadow_args())
    [res] = fakes.of("shadow.result")
    assert (res["state"], res["reason"]) == ("skipped", sh.NO_TRAFFIC)
    assert fakes.announced[0]["shadow"]["state"] == "skip"


async def test_collector_failure_is_skipped_unavailable_and_still_stops() -> None:
    fakes = ShadowFakes(summary_fails=True)
    result, _, _ = await run(fakes, shadow_args())
    assert result["status"] == "done"
    [res] = fakes.of("shadow.result")
    assert (res["state"], res["reason"]) == ("skipped", sh.UNAVAILABLE)
    assert fakes.shadow_names()[-1] == "shadow_stop"


async def test_refused_start_is_skipped_unavailable_and_still_stops() -> None:
    fakes = ShadowFakes(start_ok=False)
    await run(fakes, shadow_args())
    assert fakes.of("shadow.result")[0]["reason"] == sh.UNAVAILABLE
    assert fakes.shadow_names() == ["shadow_start", "shadow_stop"]


async def test_replication_leaving_streaming_stops_the_rehearsal() -> None:
    # 관찰 중 감시 표본이 catchup이면 즉시 멈춘다
    fakes = ShadowFakes(phases=("copy", "streaming", "catchup", "streaming"))
    result, _, _ = await run(fakes, shadow_args(window_s=60))
    assert result["status"] == "done"
    [res] = fakes.of("shadow.result")
    assert (res["state"], res["reason"]) == ("stopped", sh.STOPPED_REPLICATION)
    assert fakes.shadow_names()[-1] == "shadow_stop"
    assert "shadow_summary" not in fakes.shadow_names()


async def test_missing_relay_queue_never_hangs() -> None:
    fakes = ShadowFakes()
    args = {**shadow_args(), "relay_queue": "no-worker-here", "shadow_call_s": 1}
    result, _, _ = await run(fakes, args)
    assert result["status"] == "done"
    assert fakes.of("shadow.result")[0]["reason"] == sh.UNAVAILABLE


async def test_shadow_history_replays() -> None:
    _, _, history = await run(ShadowFakes(), shadow_args())
    await Replayer(workflows=[DataMigrationWorkflow]).replay_workflow(history)


async def test_history_from_before_the_patch_replays(monkeypatch: pytest.MonkeyPatch) -> None:
    """패치 전 코드로 만든 이력(shadow 입력이 있어도 관찰 없음)을 새 코드가 그대로 재생한다."""
    original = workflow.patched
    monkeypatch.setattr(dmw.workflow, "patched",
                        lambda pid: False if pid == dmw.SHADOW_PATCH else original(pid))  # fmt: skip
    fakes = ShadowFakes()
    _, _, history = await run(fakes, shadow_args())
    assert fakes.shadow_calls == []
    monkeypatch.setattr(dmw.workflow, "patched", original)
    await Replayer(workflows=[DataMigrationWorkflow]).replay_workflow(history)


async def test_invalid_shadow_input_is_skipped_not_a_failure() -> None:
    fakes = ShadowFakes()
    result, _, _ = await run(fakes, shadow_args(window_s=-1))
    assert result["status"] == "done"
    assert fakes.of("shadow.result")[0]["reason"] == sh.UNAVAILABLE
    assert fakes.shadow_names() == ["shadow_stop"]
