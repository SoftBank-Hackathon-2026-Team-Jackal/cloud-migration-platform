from cmp_contracts.analysis import MappingRow
from cmp_testsuite import CaseResult, Expect, HttpStep, Suite, SuiteCase, SuiteResult
from cmp_worker.sandbox_judge import coverage_report, judge
from cmp_worker.sandbox_network import EgressRecord


def results(*pairs):
    return SuiteResult(
        suite_sha256="a" * 64,
        results=[CaseResult(id=i, status=s, reason=None) for i, s in pairs],
    )


def block(host, port=443, decision="BLOCK"):
    return EgressRecord(decision=decision, host=host, port=port, count=1)


def test_all_required_tests_passing_with_no_blocked_calls_passes():
    verdict = judge(
        results=results(("a", "PASSED"), ("b", "SKIPPED")),
        egress=[block("api.pay.example.com", decision="ALLOW")],
    )
    assert verdict.passed and verdict.reasons == []


def test_one_failed_test_fails_the_verdict_and_names_it():
    verdict = judge(results=results(("a", "PASSED"), ("b", "FAILED")), egress=[])
    assert not verdict.passed and verdict.reasons == ["TEST_FAILED:b"]


def test_no_passing_test_never_passes():
    assert judge(results=results(), egress=[]).reasons == ["NO_TESTS_PASSED"]
    assert not judge(results=results(("a", "SKIPPED")), egress=[]).passed


def test_a_blocked_call_to_a_migrated_source_names_the_host():
    verdict = judge(
        results=results(("a", "PASSED")),
        egress=[block("orders.abc.ap-northeast-2.rds.amazonaws.com", 5432)],
        source_hosts={"abc.ap-northeast-2.rds.amazonaws.com"},
    )
    assert verdict.reasons == [
        "MIGRATE_SOURCE_CALLED:orders.abc.ap-northeast-2.rds.amazonaws.com:5432"
    ]


def test_a_blocked_call_outside_the_list_is_a_missing_intent_with_the_host():
    verdict = judge(results=results(("a", "PASSED")), egress=[block("telemetry.example.net")])
    assert not verdict.passed and verdict.reasons == ["EGRESS_BLOCKED:telemetry.example.net:443"]


def test_unsupported_call_patterns_from_logs_fail_the_verdict():
    assert judge(
        results=results(("a", "PASSED")), egress=[], log_hits=["sqs.SendMessage"]
    ).reasons == ["UNSUPPORTED_CALL:sqs.SendMessage"]


def test_the_same_inputs_always_give_the_same_reasons_in_the_same_order():
    egress = [block("z.example.com"), block("a.example.com")]
    first = judge(results=results(("b", "FAILED"), ("a", "FAILED")), egress=egress)
    second = judge(results=results(("a", "FAILED"), ("b", "FAILED")), egress=list(reversed(egress)))
    assert first == second
    assert first.reasons == sorted(first.reasons)


def row(capability, disposition="MIGRATE"):
    return MappingRow(
        id=f"r-{capability}", capability=capability, usage="u", grade="DIRECT",
        required_proposed=True, required_reason="r", required=None, disposition=disposition,
        loses=[], treatment=None, status="UNVERIFIED", evidence=[],
    )  # fmt: skip


def test_coverage_lists_migrated_capabilities_without_a_declaring_test():
    case = SuiteCase(
        id="pg", source="STANDARD", effect="READ_ONLY", covers=["postgres"],
        steps=[HttpStep(path="/x", expect=Expect(status=200))],
    )  # fmt: skip
    report = coverage_report(
        [row("postgres"), row("s3", "KEEP"), row("ingress")], Suite(name="d", cases=[case])
    )
    assert report.covered == {"postgres": ["pg"]} and report.uncovered == ["ingress"]
