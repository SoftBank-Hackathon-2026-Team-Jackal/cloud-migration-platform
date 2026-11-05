import json

from cmp_contracts.verification import Verdict
from cmp_worker.failure_classifier import AttemptSignals, classify_failure, find_render_mismatches

from .test_deploy import FakeRunner, make_service, requirements
from .test_update import plan_with


def fail(*reasons):
    return Verdict(passed=False, reasons=list(reasons))


def klass(**kw):
    return classify_failure(AttemptSignals(**kw))


def test_a_wrong_port_that_fails_the_health_check_with_no_app_stack_is_a_spec_problem():
    result = klass(sandbox_error="HEALTH_CHECK_FAILED", verdict=fail("TEST_FAILED:std-health"))
    assert result.failure_class == "SPEC" and result.fixer_allowed


def test_a_missing_intent_shows_up_as_a_blocked_call_and_is_spec():
    result = klass(
        verdict=fail("EGRESS_BLOCKED:api.pay.example.com:443", "TEST_FAILED:pay"),
        stack_in_app_code=True,
    )
    assert result.failure_class == "SPEC" and result.reasons == [
        "EGRESS_BLOCKED:api.pay.example.com:443"
    ]


def test_a_blocked_call_to_a_migrated_source_is_spec_too():
    assert (
        klass(verdict=fail("MIGRATE_SOURCE_CALLED:db.rds.amazonaws.com:5432")).failure_class
        == "SPEC"
    )


def test_a_failing_test_with_an_application_stack_is_a_code_problem():
    result = klass(verdict=fail("TEST_FAILED:orders"), stack_in_app_code=True)
    assert (result.failure_class, result.reasons) == ("CODE", ["TEST_FAILED:orders"])
    assert result.fixer_allowed


def test_a_failed_build_is_a_build_problem():
    assert klass(build_failure_class="BUILD").failure_class == "BUILD"


def test_a_render_that_differs_from_the_requirements_stops_without_the_fixer():
    result = klass(
        render_mismatches=["orders-api.port"],
        verdict=fail("TEST_FAILED:std-health"),
        stack_in_app_code=True,
    )
    assert result.failure_class == "RENDER" and not result.fixer_allowed
    assert result.reasons == ["RENDER_MISMATCH:orders-api.port"]
    assert klass(sandbox_error="DB_VERSION_MISMATCH").failure_class == "RENDER"


def test_environment_failures_are_infra_and_never_reach_the_fixer():
    cases = [
        {"sandbox_error": "CLUSTER_COMMAND_FAILED"},
        {"pod_events": ["ImagePullBackOff"]},
        {"build_failure_class": "INFRA"},
        {"infra_timeout": True},
    ]
    for case in cases:
        result = klass(verdict=fail("TEST_FAILED:x"), stack_in_app_code=True, **case)
        assert result.failure_class == "INFRA" and not result.fixer_allowed, case


def test_a_failed_rollout_with_an_application_cause_is_not_infra():
    spec = klass(sandbox_error="CLUSTER_COMMAND_FAILED", infra_timeout=True, app_cause_in_pod=True)
    assert spec.failure_class == "SPEC" and spec.fixer_allowed
    crash = klass(
        sandbox_error="CLUSTER_COMMAND_FAILED", app_cause_in_pod=True, stack_in_app_code=True
    )
    assert crash.failure_class == "CODE"
    bare = klass(sandbox_error="CLUSTER_COMMAND_FAILED", infra_timeout=True)
    assert bare.failure_class == "INFRA"


def test_a_timeout_with_an_application_cause_is_not_infra():
    result = klass(
        infra_timeout=True,
        app_cause_in_pod=True,
        verdict=fail("TEST_FAILED:x"),
        stack_in_app_code=True,
    )
    assert result.failure_class == "CODE"


def test_infra_wins_over_everything_else_and_the_result_is_deterministic():
    signals = AttemptSignals(
        sandbox_error="CLUSTER_COMMAND_FAILED", render_mismatches=["a.port"],
        build_failure_class="BUILD", verdict=fail("EGRESS_BLOCKED:x:1"),
    )  # fmt: skip
    assert classify_failure(signals) == classify_failure(signals)
    assert classify_failure(signals).failure_class == "INFRA"


def test_nothing_recognisable_falls_back_to_spec():
    assert klass(verdict=fail("NO_TESTS_PASSED")).failure_class == "SPEC"


def test_the_rendered_request_matches_the_requirements_it_came_from(tmp_path):
    plan = plan_with(make_service(FakeRunner()), tmp_path)
    assert find_render_mismatches(requirements(), plan.request) == []


def test_a_changed_port_or_database_version_is_reported_per_field(tmp_path):
    plan = plan_with(make_service(FakeRunner()), tmp_path)
    request = json.loads(json.dumps(plan.request))
    for resource in request["resources"]:
        if resource["kind"] == "APPLICATION":
            resource["port"] = 9999
        else:
            resource["version"] = "17"
    assert find_render_mismatches(requirements(), request) == [
        "orders-api.port",
        "orders-db.version",
    ]
    request["resources"] = [r for r in request["resources"] if r["id"] != "orders-api"]
    assert find_render_mismatches(requirements(), request) == [
        "orders-api:missing",
        "orders-db.version",
    ]
