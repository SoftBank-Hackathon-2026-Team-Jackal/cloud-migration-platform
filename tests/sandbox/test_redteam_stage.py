"""Ticket 37: the red-team Activity's body, with every outside piece faked.

`run_stage` is where tickets 33-36 meet. These tests pin the order and the guarantees of that
meeting: a start that is not allowed attacks nothing, a Finding is only kept with clean evidence, a
re-check replays only that Finding's own request, and a dead executor never counts as "fixed".
"""

import pytest
from cmp_agents.redteam_agent import AllowList, Decision, ObservedResult, ToolCall
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.redteam_safeguards import (
    Finding,
    InMemoryApprovalLedger,
    Intensity,
    RedTeamRunner,
)
from cmp_worker.redteam_stage import (
    NOT_CONFIGURED,
    RedTeamInput,
    RedTeamKit,
    request_for,
    run_stage,
)

from .test_redteam_safeguards import (
    DIGEST,
    NOW,
    SECRET,
    SUITE,
    FakeIsolation,
    FakeService,
    at,
    sandbox_plan,
)

NEW_DIGEST = "sha256:" + "c" * 64
RED = RedTeamInput(
    intensity=Intensity.PASSIVE,
    time_budget_seconds=120,
    target_resource_id="orders-api",
    consent_ref="consent-1",
    endpoints=[{"method": "GET", "path": "/items", "params": ["id"]}],
    max_turns=3,
)
DRAFT = {
    "category": "AUTHZ",
    "severity": "HIGH",
    "endpoint": "/items",
    "summary": "any user reads another user's item",
    "repro_summary": "GET /items?id=2 without a token",
    "evidence": "HTTP 200 body: {'owner': 2}",
    "repro": {"tool": "probe_get", "path": "/items?id=2"},
}


class Env:
    """Fakes for the model, the tool runner and the judge; records what reached the executor."""

    def __init__(self, tmp_path, *, drafts=None, still_reproduces=True, dispatch_fails=False):
        self.plan = sandbox_plan(tmp_path)
        self.events: list[str] = []
        self.sent: list[ToolCall] = []
        self.drafts = [DRAFT] if drafts is None else drafts
        self.still_reproduces = still_reproduces
        self.dispatch_fails = dispatch_fails
        self.store = LocalArtifactStore(tmp_path / "artifacts")
        self.decided = 0

    def kit(self, *, left=3600, ledger=None):
        env = self
        runner = RedTeamRunner(
            FakeService(self.events), FakeIsolation(self.events),
            ledger or InMemoryApprovalLedger(), clock=lambda: at(self.plan, left),
        )  # fmt: skip

        class FakeBrain:
            error = None

            def decide(self, agent_input):
                def turn(history):
                    env.decided += 1
                    if env.decided > 1:
                        return Decision(call=None)
                    return Decision(call={"tool": "probe_get", "path": "/items?id=2"})

                return turn

            def detect(self, agent_input, loop):
                return list(env.drafts)

            def reproduces(self, finding, observed):
                return env.still_reproduces

        def dispatch(executor, secrets):
            def call(tool_call):
                env.events.append("dispatch")
                if env.dispatch_fails:
                    raise RuntimeError("executor gone")
                env.sent.append(tool_call)
                return ObservedResult.from_raw("HTTP 200 {'owner': 2}")

            return call

        return RedTeamKit(
            runner=runner,
            store=self.store,
            allowed=AllowList({"probe_get"}),
            brain=lambda migration_id: FakeBrain(),
            dispatch=dispatch,
            known_secrets=lambda plan: [SECRET],
        )

    def approval(self, **changes):
        base = {
            "approval_id": "appr-1",
            "approver": "park",
            "approved_at": NOW.isoformat(),
            "run_id": self.plan.run_id,
            "intensity": "PASSIVE",
            "time_budget_seconds": 120,
        }
        return base | changes

    def stage(self, kit, *, previous=None, approval="default", digest=DIGEST):
        return run_stage(
            kit,
            plan=self.plan,
            red=RED,
            approval=self.approval() if approval == "default" else approval,
            digest=digest,
            suite_sha256=SUITE,
            requirements_text="the app lists items",
            previous=previous or [],
            clock=lambda: NOW,
        )


def test_a_worker_without_a_kit_attacks_nothing_and_says_so(tmp_path):
    env = Env(tmp_path)
    result = env.stage(None)
    assert result["status"] == "SKIPPED" and result["code"] == NOT_CONFIGURED
    assert env.events == []


def test_a_run_without_a_valid_yes_attacks_nothing(tmp_path):
    env = Env(tmp_path)
    for bad in (None, {"approval_id": "x"}):
        result = env.stage(env.kit(), approval=bad)
        assert result["status"] == "SKIPPED" and result["code"] == "REDTEAM_NOT_APPROVED"
    mismatched = env.stage(env.kit(), approval=env.approval(time_budget_seconds=600))
    assert mismatched["code"] == "REDTEAM_APPROVAL_MISMATCH"
    assert "dispatch" not in env.events and "isolated" not in env.events


def test_a_run_that_is_not_isolated_attacks_nothing(tmp_path):
    env = Env(tmp_path)
    kit = env.kit()
    kit.runner._isolation._fails_with = "REDTEAM_ISOLATION_NOT_ENFORCED"
    result = env.stage(kit)
    assert result["status"] == "SKIPPED" and result["code"] == "REDTEAM_ISOLATION_NOT_ENFORCED"
    assert "dispatch" not in env.events


def test_the_first_run_explores_inside_the_safeguards_and_keeps_a_finding_with_its_request(
    tmp_path,
):
    env = Env(tmp_path)
    result = env.stage(env.kit())
    assert result["status"] == "DONE" and result["code"] is None
    assert env.events[:2] == ["isolated", "cut"] and env.events[-2:] == [
        "uncut",
        "executor-removed",
    ]
    assert [c.path for c in env.sent] == ["/items?id=2"]
    (kept,) = result["open"]
    assert kept["image_digest"] == DIGEST and kept["suite_sha256"] == SUITE
    assert kept["repro"] == {"tool": "probe_get", "path": "/items?id=2"}
    assert kept["evidence_ref"] is not None  # the evidence is an artifact, not text in the report
    assert result["summary"]["count"] == 1 and result["stopped_reason"] == "DONE"
    assert Finding.model_validate({k: v for k, v in kept.items() if k != "repro"})


def test_the_same_weakness_keeps_one_id_so_a_later_image_can_be_asked_about_it(tmp_path):
    env = Env(tmp_path, drafts=[DRAFT, dict(DRAFT)])
    first = env.stage(env.kit())
    assert len(first["open"]) == 1  # two identical drafts are one Finding
    env.events.clear()
    second = env.stage(env.kit(), approval=env.approval(approval_id="appr-2"), digest=NEW_DIGEST)
    assert second["open"][0]["finding_id"] == first["open"][0]["finding_id"]


def test_evidence_that_still_holds_a_secret_is_withheld_and_no_finding_is_kept(tmp_path):
    leaky = {**DRAFT, "evidence": f"shouted: {SECRET.upper()}"}
    env = Env(tmp_path, drafts=[leaky])
    result = env.stage(env.kit())
    assert result["open"] == [] and result["summary"]["count"] == 0
    assert [w["code"] for w in result["withheld"]] == ["SECRET_IN_EVIDENCE"]
    assert SECRET.upper() not in str(result)


def test_a_finding_text_never_carries_the_apps_secret(tmp_path):
    drafts = [
        {**DRAFT, "summary": f"the page prints {SECRET}", "repro_summary": f"GET /x {SECRET}"}
    ]
    env = Env(tmp_path, drafts=drafts)
    result = env.stage(env.kit())
    assert SECRET not in str(result)


def test_a_draft_that_points_outside_the_app_is_refused(tmp_path):
    env = Env(
        tmp_path, drafts=[{**DRAFT, "repro": {"tool": "probe_get", "path": "http://other/x"}}]
    )
    with pytest.raises(ValueError):
        env.stage(env.kit())
    assert env.events[-2:] == ["uncut", "executor-removed"]  # and the safeguards still unwound


def test_a_recheck_replays_only_the_earlier_findings_request_on_the_new_digest(tmp_path):
    env = Env(tmp_path, still_reproduces=False)
    first = env.stage(env.kit())
    env.sent.clear()
    env.events.clear()
    asked_before = env.decided
    second = env.stage(
        env.kit(ledger=InMemoryApprovalLedger()),
        previous=first["open"],
        approval=env.approval(approval_id="appr-2"),
        digest=NEW_DIGEST,
    )
    assert [c.path for c in env.sent] == ["/items?id=2"]  # that request, nothing new
    assert env.decided == asked_before  # the model was not asked again
    assert second["open"] == [] and second["resolved"] == [first["open"][0]["finding_id"]]


def test_a_finding_that_still_reproduces_stays_open_on_the_new_digest(tmp_path):
    env = Env(tmp_path, still_reproduces=True)
    first = env.stage(env.kit())
    second = env.stage(
        env.kit(),
        previous=first["open"],
        approval=env.approval(approval_id="appr-2"),
        digest=NEW_DIGEST,
    )
    (still,) = second["open"]
    assert still["image_digest"] == NEW_DIGEST and still["repro"] == first["open"][0]["repro"]
    assert second["resolved"] == []


def test_a_dead_executor_during_a_recheck_never_marks_a_finding_fixed(tmp_path):
    env = Env(tmp_path)
    first = env.stage(env.kit())
    env.dispatch_fails = True
    second = env.stage(
        env.kit(),
        previous=first["open"],
        approval=env.approval(approval_id="appr-2"),
        digest=NEW_DIGEST,
    )
    assert [f["finding_id"] for f in second["open"]] == [first["open"][0]["finding_id"]]
    assert second["resolved"] == [] and second["stopped_reason"] == "EXECUTOR_ERROR"


def test_a_used_approval_is_refused_the_second_time(tmp_path):
    env = Env(tmp_path)
    ledger = InMemoryApprovalLedger()
    assert env.stage(env.kit(ledger=ledger))["status"] == "DONE"
    again = env.stage(env.kit(ledger=ledger))
    assert again["status"] == "SKIPPED" and again["code"] == "REDTEAM_APPROVAL_USED"


def test_a_reset_that_fails_after_an_active_run_is_reported_with_what_the_run_found(tmp_path):
    env = Env(tmp_path)
    kit = env.kit()
    kit.runner._service._reset_fails = True
    red = RedTeamInput(**{**RED.model_dump(), "intensity": Intensity.ACTIVE})
    result = run_stage(
        kit, plan=env.plan, red=red, approval=env.approval(intensity="ACTIVE"), digest=DIGEST,
        suite_sha256=SUITE, requirements_text="", previous=[], clock=lambda: NOW,
    )  # fmt: skip
    assert result["status"] == "ERROR" and result["code"] == "RESET_FAILED"
    assert len(result["open"]) == 1  # the finding is not thrown away with the failure


def test_the_request_tells_the_approver_how_long_the_sandbox_leaves_for_an_answer(tmp_path):
    env = Env(tmp_path)
    shown = request_for(env.plan, RED, now=env.plan.expires_at - 1000)
    assert shown["request"]["target"] == env.plan.namespace
    assert shown["request"]["intensity"] == "PASSIVE"
    assert shown["wait_seconds"] == 1000 - 120 - 120  # budget and the safety margin are kept back
    late = request_for(env.plan, RED, now=env.plan.expires_at - 100)
    assert late["wait_seconds"] == 0


def test_the_fixer_reads_a_masked_summary_of_each_finding_and_nothing_to_attack_with():
    from cmp_agents.fixer import FixInput, _task

    prompt = _task(
        FixInput(
            failure_class="SECURITY",
            reasons=["FINDING:authz-1"],
            findings=[
                {
                    "finding_id": "authz-1", "category": "AUTHZ", "severity": "HIGH",
                    "endpoint": "/items", "summary": "any user reads another user's item",
                    "repro_summary": "GET /items?id=2 key AKIAABCDEFGHIJKLMNOP",
                }
            ],
        )
    )  # fmt: skip
    assert "Security findings to fix" in prompt and "[HIGH] AUTHZ /items" in prompt
    assert "AKIAABCDEFGHIJKLMNOP" not in prompt
    plain = _task(FixInput(failure_class="CODE", reasons=["x"]))
    assert "Security findings" not in plain  # an ordinary fix reads exactly what it did before


def test_the_loop_registers_the_two_red_team_activities():
    from cmp_worker.activities.sandbox_loop import SandboxLoopActivities

    names = {
        a.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for a in SandboxLoopActivities.all(object.__new__(SandboxLoopActivities))
    }
    assert {"sbx_redteam_request", "sbx_redteam"} <= names
