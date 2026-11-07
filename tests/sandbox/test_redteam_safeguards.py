"""Ticket 34: safeguards around a red-team run, and the contract of what a run reports.

Part 1 (this block): the Finding contract and the masking of evidence before anything is stored.
Seams: pure models, and `store_evidence` on the local artifact store. Nothing here talks to a
cluster. The preflight (part 2) and the run wrapper (part 3) are tested further down.
"""

import base64
import json
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.redteam_safeguards import (
    Finding,
    Intensity,
    Severity,
    make_finding,
    store_evidence,
    summarize,
)
from pydantic import ValidationError

DIGEST = "sha256:" + "a" * 64
SUITE = "b" * 64
SECRET = "Zx9-live-key-4471"  # an app's real env value, as the Sandbox Secret holds it
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def finding(**changes):
    fields = {
        "finding_id": "f-1",
        "category": "input-validation",
        "severity": Severity.HIGH,
        "endpoint": "/orders",
        "summary": "A numeric field accepts text.",
        "repro_summary": "POST /orders with a text value in `quantity`.",
        "evidence_ref": None,
        "image_digest": DIGEST,
        "suite_sha256": SUITE,
        "intensity": Intensity.PASSIVE,
        "executed_at": NOW,
    }
    return Finding(**(fields | changes))


# --- the Finding contract -----------------------------------------------------------------------


def test_a_finding_always_names_the_image_and_the_suite_it_was_found_against():
    assert finding().image_digest == DIGEST and finding().suite_sha256 == SUITE
    for field in ("image_digest", "suite_sha256"):
        with pytest.raises(ValidationError):
            Finding(**{k: v for k, v in finding().model_dump().items() if k != field})
    for bad in ("", "latest", "sha256:abc", "sha256:" + "G" * 64):
        with pytest.raises(ValidationError):
            finding(image_digest=bad)
    with pytest.raises(ValidationError):
        finding(suite_sha256="not-a-hash")


def test_a_finding_points_at_a_path_inside_the_app_and_never_at_a_host():
    assert finding(endpoint="/api/v1/items/{id}").endpoint == "/api/v1/items/{id}"
    for bad in ("http://example.com/x", "example.com/x", "//example.com/x", "10.0.0.1", ""):
        with pytest.raises(ValidationError):
            finding(endpoint=bad)


def test_the_free_text_fields_are_short():
    with pytest.raises(ValidationError):
        finding(summary="x" * 301)
    with pytest.raises(ValidationError):
        finding(repro_summary="x" * 501)


def test_a_finding_survives_a_json_round_trip_and_rejects_unknown_fields():
    original = finding()
    assert Finding.model_validate_json(original.model_dump_json()) == original
    with pytest.raises(ValidationError):
        Finding.model_validate({**json.loads(original.model_dump_json()), "raw_response": "..."})


def test_make_finding_masks_secret_looking_text_in_the_summaries():
    made = make_finding(
        finding_id="f-2",
        category="information-exposure",
        severity=Severity.CRITICAL,
        endpoint="/debug",
        summary="The page prints AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP.",
        repro_summary="GET /debug returns password=hunter2hunter2 in the body.",
        known_secrets=["hunter2hunter2"],
        image_digest=DIGEST,
        suite_sha256=SUITE,
        intensity=Intensity.ACTIVE,
        executed_at=NOW,
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in made.summary
    assert "hunter2hunter2" not in made.repro_summary
    assert made.evidence_ref is None


# --- evidence -----------------------------------------------------------------------------------


def stored_text(store, result):
    return store.get(result.ref).decode()


def test_evidence_is_stored_without_the_apps_secret_in_any_common_form(tmp_path):
    store = LocalArtifactStore(tmp_path)
    forms = [
        SECRET,
        quote(SECRET, safe=""),
        base64.b64encode(SECRET.encode()).decode(),
        base64.urlsafe_b64encode(SECRET.encode()).decode(),
    ]
    evidence = "\n".join(f"line {i}: {form}" for i, form in enumerate(forms))
    result = store_evidence(store, finding_id="f-1", evidence=evidence, known_secrets=[SECRET])
    assert result.status == "STORED" and result.ref is not None
    text = stored_text(store, result)
    for form in forms:
        assert form not in text
    assert "line 0:" in text  # the rest of the evidence is kept


def test_evidence_with_a_known_key_pattern_is_masked_even_without_a_known_value(tmp_path):
    store = LocalArtifactStore(tmp_path)
    result = store_evidence(
        store, finding_id="f-1", evidence="id=AKIAABCDEFGHIJKLMNOP", known_secrets=[]
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in stored_text(store, result)


def test_evidence_that_still_shows_the_secret_in_a_form_we_cannot_redact_is_not_stored(tmp_path):
    store = LocalArtifactStore(tmp_path)
    result = store_evidence(
        store, finding_id="f-1", evidence=f"shouted: {SECRET.upper()}", known_secrets=[SECRET]
    )
    assert result.status == "SECRET_IN_EVIDENCE" and result.ref is None
    assert list(tmp_path.rglob("*")) == [] or all(p.is_dir() for p in tmp_path.rglob("*"))


def test_the_evidence_reference_is_content_addressed_and_typed(tmp_path):
    store = LocalArtifactStore(tmp_path)
    one = store_evidence(store, finding_id="f-1", evidence="same", known_secrets=[])
    two = store_evidence(store, finding_id="f-1", evidence="same", known_secrets=[])
    assert one.ref == two.ref and one.ref.kind == "redteam-evidence"
    assert one.ref.id == "redteam-evidence-f-1"


def test_very_short_values_are_not_treated_as_secrets(tmp_path):
    # "true" or "8080" are env values too, but redacting them would destroy every response.
    store = LocalArtifactStore(tmp_path)
    result = store_evidence(
        store, finding_id="f-1", evidence="debug=true port=8080", known_secrets=["true", "8080"]
    )
    assert stored_text(store, result) == "debug=true port=8080"


# --- what the workflow history gets --------------------------------------------------------------


def test_the_summary_for_the_workflow_history_holds_counts_and_ids_only():
    findings = [
        finding(finding_id="f-1", severity=Severity.LOW),
        finding(finding_id="f-2", severity=Severity.CRITICAL, summary="SECRET TEXT HERE"),
        finding(finding_id="f-3", severity=Severity.LOW),
    ]
    summary = summarize(findings)
    assert summary.count == 3 and summary.max_severity == Severity.CRITICAL
    assert summary.by_severity == {"LOW": 2, "CRITICAL": 1}
    assert summary.finding_ids == ["f-1", "f-2", "f-3"]
    dumped = summary.model_dump_json()
    assert "SECRET TEXT HERE" not in dumped and "/orders" not in dumped


def test_an_empty_run_summarizes_to_nothing():
    summary = summarize([])
    assert summary.count == 0 and summary.max_severity is None and summary.finding_ids == []


# ==================================================================================================
# Part 2: what must be true before a run starts (approval, time, baseline)
# ==================================================================================================

from cmp_worker.activities.k8s_workloads_cli import CommandError
from cmp_worker.redteam_safeguards import (
    SAFETY_MARGIN_SECONDS,
    Approval,
    InMemoryApprovalLedger,
    RunConfig,
    approval_request,
    check_start,
)
from cmp_worker.sandbox_deploy import SandboxError

from .test_deploy import FakeRunner, make_service
from .test_update import plan_with


def sandbox_plan(tmp_path):
    return plan_with(make_service(FakeRunner()), tmp_path)


def at(plan, seconds_left):
    return datetime.fromtimestamp(plan.expires_at - seconds_left, UTC)


def approved(plan, config, **changes):
    fields = {
        "approval_id": "appr-1",
        "approver": "owner@example.com",
        "approved_at": NOW,
        "run_id": plan.run_id,
        "intensity": config.intensity,
        "time_budget_seconds": config.time_budget_seconds,
    }
    return Approval(**(fields | changes))


def start(plan, config, approval, *, left=3600, baseline=True, ledger=None):
    ledger = ledger or InMemoryApprovalLedger()
    check_start(
        plan, config, approval, now=at(plan, left), baseline_exists=lambda: baseline, ledger=ledger
    )
    return ledger


def refusal(plan, config, approval, **kw):
    with pytest.raises(SandboxError) as caught:
        start(plan, config, approval, **kw)
    return str(caught.value)


PASSIVE = RunConfig(intensity=Intensity.PASSIVE, time_budget_seconds=120)
ACTIVE = RunConfig(intensity=Intensity.ACTIVE, time_budget_seconds=300)


def test_a_run_needs_an_approval_that_matches_what_will_actually_run(tmp_path):
    plan = sandbox_plan(tmp_path)
    assert refusal(plan, PASSIVE, None) == "REDTEAM_NOT_APPROVED"
    start(plan, PASSIVE, approved(plan, PASSIVE))  # the matching approval starts
    for changes in (
        {"run_id": "another-run"},
        {"intensity": Intensity.ACTIVE},
        {"time_budget_seconds": 119},
        {"time_budget_seconds": 121},
    ):
        assert refusal(plan, PASSIVE, approved(plan, PASSIVE, **changes)) == (
            "REDTEAM_APPROVAL_MISMATCH"
        ), changes


def test_an_approval_works_for_one_run_only(tmp_path):
    plan = sandbox_plan(tmp_path)
    ledger = start(plan, PASSIVE, approved(plan, PASSIVE))
    assert refusal(plan, PASSIVE, approved(plan, PASSIVE), ledger=ledger) == (
        "REDTEAM_APPROVAL_USED"
    )


def test_a_refused_start_does_not_use_up_the_approval(tmp_path):
    plan = sandbox_plan(tmp_path)
    ledger = InMemoryApprovalLedger()
    assert refusal(plan, ACTIVE, approved(plan, ACTIVE), baseline=False, ledger=ledger) == (
        "REDTEAM_BASELINE_MISSING"
    )
    start(plan, ACTIVE, approved(plan, ACTIVE), ledger=ledger)  # still usable


def test_the_sandbox_must_outlive_the_budget_plus_the_time_to_reset_and_clean_up(tmp_path):
    plan = sandbox_plan(tmp_path)
    need = PASSIVE.time_budget_seconds + SAFETY_MARGIN_SECONDS
    start(plan, PASSIVE, approved(plan, PASSIVE), left=need)
    assert refusal(plan, PASSIVE, approved(plan, PASSIVE), left=need - 1) == (
        "REDTEAM_TIME_INSUFFICIENT"
    )
    assert refusal(plan, PASSIVE, approved(plan, PASSIVE), left=-5) == "REDTEAM_TIME_INSUFFICIENT"


def test_time_is_judged_when_the_run_starts_not_when_it_was_approved(tmp_path):
    # Waiting for the approval used up the Sandbox's time: the same approval is now too late.
    plan = sandbox_plan(tmp_path)
    approval = approved(plan, PASSIVE)
    start(plan, PASSIVE, approval, left=3600)
    assert refusal(plan, PASSIVE, approval, left=60) == "REDTEAM_TIME_INSUFFICIENT"


def test_only_an_active_run_needs_a_baseline(tmp_path):
    plan = sandbox_plan(tmp_path)
    start(plan, PASSIVE, approved(plan, PASSIVE), baseline=False)
    assert refusal(plan, ACTIVE, approved(plan, ACTIVE), baseline=False) == (
        "REDTEAM_BASELINE_MISSING"
    )
    start(plan, ACTIVE, approved(plan, ACTIVE), baseline=True)


def test_the_baseline_is_not_even_looked_for_when_the_run_is_passive(tmp_path):
    plan = sandbox_plan(tmp_path)

    def must_not_be_called():
        raise AssertionError("a passive run never touches the database")

    check_start(
        plan, PASSIVE, approved(plan, PASSIVE), now=at(plan, 3600),
        baseline_exists=must_not_be_called, ledger=InMemoryApprovalLedger(),
    )  # fmt: skip


def test_the_budget_is_bounded():
    for seconds in (0, 29, 3601):
        with pytest.raises(ValidationError):
            RunConfig(intensity=Intensity.PASSIVE, time_budget_seconds=seconds)
    with pytest.raises(ValidationError):
        RunConfig(intensity="EXHAUST", time_budget_seconds=60)


def test_the_approval_request_tells_the_approver_what_will_happen(tmp_path):
    plan = sandbox_plan(tmp_path)
    request = approval_request(plan, ACTIVE)
    assert request["run_id"] == plan.run_id and request["target"] == plan.namespace
    assert request["intensity"] == "ACTIVE" and request["time_budget_seconds"] == 300
    assert request["test_data_is_reset_afterwards"] is True
    assert request["outside_calls_are_cut_meanwhile"] is True
    assert request["scope"] == "this Sandbox copy of the application only"
    passive = approval_request(plan, PASSIVE)
    assert passive["test_data_is_reset_afterwards"] is False


def test_the_approval_request_carries_no_secret(tmp_path):
    plan = sandbox_plan(tmp_path)
    assert "generated-db-password" not in json.dumps(approval_request(plan, ACTIVE))


# --- the baseline lookup on the Sandbox service ----------------------------------------------------


class NoBaselineRunner(FakeRunner):
    def run(self, args, *, stdin=None, timeout=300, env=None):
        if "test -s" in " ".join(args):
            raise CommandError("kubectl")
        return super().run(args, stdin=stdin, timeout=timeout, env=env)


def test_the_service_reports_whether_a_baseline_exists(tmp_path):
    with_baseline = make_service(FakeRunner())
    assert with_baseline.baseline_exists(plan_with(with_baseline, tmp_path)) is True
    without = make_service(NoBaselineRunner())
    assert without.baseline_exists(plan_with(without, tmp_path)) is False


# ==================================================================================================
# Part 3: the run wrapper (order of things, and what always happens afterwards)
# ==================================================================================================

from contextlib import contextmanager

from cmp_worker.redteam_safeguards import RedTeamRunner
from cmp_worker.sandbox_isolation import Executor

EXECUTOR = Executor(name="cmp-redteam-x", namespace="ns", target_ip="10.43.0.40", port=8080)


class FakeService:
    """Records the order of what the wrapper does to the Sandbox."""

    def __init__(self, events, *, cut_fails=False, reset_fails=False, baseline=True):
        self.events, self._cut_fails = events, cut_fails
        self._reset_fails, self._baseline = reset_fails, baseline

    @contextmanager
    def external_cut(self, plan):
        if self._cut_fails:
            raise SandboxError("CLUSTER_COMMAND_FAILED")
        self.events.append("cut")
        try:
            yield
        finally:
            self.events.append("uncut")

    def reset_test_data(self, plan):
        self.events.append("reset")
        if self._reset_fails:
            raise SandboxError("RESET_FAILED")
        return {"reset": "OK"}

    def baseline_exists(self, plan):
        return self._baseline


class FakeIsolation:
    def __init__(self, events, *, fails_with=None):
        self.events, self._fails_with = events, fails_with

    @contextmanager
    def session(self, plan, *, target_resource_id):
        if self._fails_with:
            raise SandboxError(self._fails_with)
        self.events.append("isolated")
        try:
            yield EXECUTOR
        finally:
            self.events.append("executor-removed")


def runner(tmp_path, *, left=3600, **kw):
    plan = sandbox_plan(tmp_path)
    events = []
    isolation_fails = kw.pop("isolation_fails", None)
    ledger = kw.pop("ledger", None) or InMemoryApprovalLedger()
    made = RedTeamRunner(
        FakeService(events, **kw), FakeIsolation(events, fails_with=isolation_fails), ledger,
        clock=lambda: at(plan, left),
    )  # fmt: skip
    return made, plan, events, ledger


def go(made, plan, config, approval=None, body=None):
    approval = approval or approved(plan, config)
    with made.run(plan, target_resource_id="orders-api", config=config, approval=approval) as run:
        if body:
            body(run)
        return run


def test_isolation_is_proved_before_the_outside_world_is_cut(tmp_path):
    # Cutting the proxy first would make it look unreachable and prove nothing about the policies.
    made, plan, events, _ = runner(tmp_path)
    go(made, plan, PASSIVE, body=lambda run: events.append("work"))
    assert events == ["isolated", "cut", "work", "uncut", "executor-removed"]


def test_an_active_run_puts_the_data_back_after_the_executor_and_the_cut_are_gone(tmp_path):
    made, plan, events, _ = runner(tmp_path)
    go(made, plan, ACTIVE, body=lambda run: events.append("work"))
    assert events == ["isolated", "cut", "work", "uncut", "executor-removed", "reset"]


def test_a_passive_run_never_resets_anything(tmp_path):
    made, plan, events, _ = runner(tmp_path)
    go(made, plan, PASSIVE)
    assert "reset" not in events


def test_the_data_is_put_back_even_when_the_work_fails(tmp_path):
    made, plan, events, _ = runner(tmp_path)

    def fails(run):
        raise RuntimeError("the work broke")

    with pytest.raises(RuntimeError, match="the work broke"):
        go(made, plan, ACTIVE, body=fails)
    assert events[-3:] == ["uncut", "executor-removed", "reset"]


def test_a_failed_reset_is_reported_and_not_hidden(tmp_path):
    made, plan, _, _ = runner(tmp_path, reset_fails=True)
    with pytest.raises(SandboxError) as caught:
        go(made, plan, ACTIVE)
    assert str(caught.value) == "RESET_FAILED"


def test_a_failed_reset_after_failed_work_still_reports_both(tmp_path):
    made, plan, _, _ = runner(tmp_path, reset_fails=True)

    def fails(run):
        raise RuntimeError("the work broke")

    with pytest.raises(SandboxError) as caught:
        go(made, plan, ACTIVE, body=fails)
    assert str(caught.value) == "RESET_FAILED"
    assert isinstance(caught.value.__context__, RuntimeError)


def test_if_the_outside_world_cannot_be_cut_nothing_runs_and_nothing_stays(tmp_path):
    made, plan, events, _ = runner(tmp_path, cut_fails=True)
    with pytest.raises(SandboxError) as caught:
        go(made, plan, ACTIVE, body=lambda run: events.append("work"))
    assert str(caught.value) == "REDTEAM_EXTERNAL_NOT_CUT"
    assert "work" not in events and events[-1] == "executor-removed"
    assert "reset" not in events  # no work started, so no data to put back


def test_an_isolation_that_does_not_hold_stops_before_the_cut(tmp_path):
    made, plan, events, _ = runner(tmp_path, isolation_fails="REDTEAM_ISOLATION_NOT_ENFORCED")
    with pytest.raises(SandboxError) as caught:
        go(made, plan, ACTIVE)
    assert str(caught.value) == "REDTEAM_ISOLATION_NOT_ENFORCED"
    assert events == []


def test_the_preflight_runs_before_anything_is_touched(tmp_path):
    cases = [
        ({"left": 60}, None, "REDTEAM_TIME_INSUFFICIENT"),
        ({"baseline": False}, ACTIVE, "REDTEAM_BASELINE_MISSING"),
    ]
    for kwargs, config, code in cases:
        made, plan, events, _ = runner(tmp_path, **kwargs)
        with pytest.raises(SandboxError) as caught:
            go(made, plan, config or ACTIVE)
        assert str(caught.value) == code and events == []
    made, plan, events, _ = runner(tmp_path)
    with (
        pytest.raises(SandboxError) as caught,
        made.run(plan, target_resource_id="orders-api", config=PASSIVE, approval=None),
    ):
        pass
    assert str(caught.value) == "REDTEAM_NOT_APPROVED" and events == []


def test_an_approval_is_used_up_by_a_run_that_started(tmp_path):
    made, plan, _, _ = runner(tmp_path)
    approval = approved(plan, PASSIVE)
    go(made, plan, PASSIVE, approval=approval)
    with pytest.raises(SandboxError) as caught:
        go(made, plan, PASSIVE, approval=approval)
    assert str(caught.value) == "REDTEAM_APPROVAL_USED"


def test_the_run_context_carries_the_deadline_and_the_one_address_the_work_may_use(tmp_path):
    made, plan, _, _ = runner(tmp_path)
    run = go(made, plan, ACTIVE)
    assert run.executor == EXECUTOR and run.intensity is Intensity.ACTIVE
    assert run.deadline == at(plan, 3600).timestamp() + ACTIVE.time_budget_seconds
