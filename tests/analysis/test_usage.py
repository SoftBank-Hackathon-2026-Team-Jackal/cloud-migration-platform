"""Ticket 06C: which AWS resources the app uses, from the user's scope, the code and CloudTrail.

Seam: pure functions in `cmp_scanners.usage` (candidate selection x usage traces -> risk codes,
scan -> `AwsUsage` evidence), plus the pipeline step that puts them in the report. Expected values
come from the inputs of each case, not from running the code.
"""

from cmp_agents.mapping import check_evidence
from cmp_contracts.analysis import (
    AnalysisInput,
    AnalysisReport,
    AwsUsage,
    ConfirmedScope,
    Evidence,
    MappingRow,
)
from cmp_scanners.aws_infra import (
    AwsInfraScan,
    CloudTrailEventSummary,
    EksCluster,
    RolePolicyInfo,
)
from cmp_scanners.code import CallSite, CodeScan, EnvComparison
from cmp_scanners.usage import (
    build_aws_usage,
    cloud_resource_locators,
    code_services,
    reconcile_usage,
    trail_services,
)
from cmp_worker.artifact_store import ArtifactRef
from pipeline_support import MAIN_GO, git, make

ROLE = "arn:aws:iam::123456789012:role/chat-api-irsa"
SELECTION = {
    "connection_id": "conn-" + "5" * 32,
    "region": "ap-northeast-2",
    "cluster": "chat-prod",
    "namespace": "chat",
    "workload": "chat-api",
    "workload_kind": "Deployment",
}


def candidate(resource_type, name, arn, *, selected):
    return {
        "candidate_id": f"{resource_type.lower()}-{name}",
        "resource_type": resource_type,
        "name": name,
        "arn": arn,
        "evidence": [],
        "suggested": True,
        "selected": selected,
    }


S3 = ("S3", "chat-attachments", "arn:aws:s3:::chat-attachments")
SQS = ("SQS", "chat-events", "arn:aws:sqs:ap-northeast-2:123456789012:chat-events")
OTHER = [
    ("IAM_ROLE", "chat-api-irsa", ROLE),
    ("ALB", "k8s-chat", "arn:aws:elasticloadbalancing:ap-northeast-2:1:loadbalancer/app/x/1"),
    ("SECRET", "chat-db", None),
    ("POSTGRESQL", "chat-db", None),
]


def scope(*items):
    """`items` are (type, name, arn, selected)."""
    return ConfirmedScope.model_validate(
        {
            "scope_id": "scope-" + "a" * 16,
            "selection": SELECTION,
            "candidates": [candidate(t, n, a, selected=sel) for t, n, a, sel in items],
        }
    )


def sdk(service, call=None):
    return CallSite(
        kind="AWS_SDK", target=f"{service}.{call}" if call else service, file="main.go", line=3
    )


# --- candidate selection x usage traces -------------------------------------------------------


def test_an_excluded_resource_that_the_code_uses_is_reported():
    risks = reconcile_usage(scope((*S3, False)), code_services={"s3"}, trail_services=set())
    assert len(risks) == 1
    assert risks[0].startswith("SCOPE_EXCLUDED_BUT_USED:")
    assert "S3" in risks[0] and "chat-attachments" in risks[0] and "code" in risks[0]


def test_an_excluded_resource_that_only_cloudtrail_shows_is_reported_with_that_source():
    risks = reconcile_usage(scope((*SQS, False)), code_services=set(), trail_services={"sqs"})
    assert len(risks) == 1
    assert risks[0].startswith("SCOPE_EXCLUDED_BUT_USED:") and "cloudtrail" in risks[0]


def test_a_selected_resource_without_any_trace_is_noted_with_the_sources_that_were_checked():
    risks = reconcile_usage(scope((*SQS, True)), code_services=set(), trail_services=set())
    assert len(risks) == 1
    assert risks[0].startswith("SCOPE_SELECTED_NO_USAGE:") and "chat-events" in risks[0]
    assert "code" in risks[0] and "cloudtrail" in risks[0]
    # No trace is not proof of no use: the wording says what was and was not visible.
    assert "CLOUDTRAIL_DATA_EVENTS_NOT_VISIBLE" in risks[0]


def test_a_selected_resource_that_is_used_gives_no_risk():
    assert reconcile_usage(scope((*S3, True)), code_services={"s3"}, trail_services=None) == []


def test_code_usage_of_a_service_without_a_candidate_is_reported():
    risks = reconcile_usage(
        scope((*S3, True)), code_services={"s3", "dynamodb"}, trail_services=None
    )
    assert risks == ["USAGE_NOT_IN_SCOPE: dynamodb (code)"]


def test_cloudtrail_alone_never_reports_usage_outside_the_scope():
    # LookupEvents covers the whole account (other principals, the platform's own calls).
    risks = reconcile_usage(
        scope((*S3, True)), code_services={"s3"}, trail_services={"s3", "dynamodb", "ec2"}
    )
    assert risks == []


def test_infrastructure_services_in_the_code_are_not_app_resources():
    risks = reconcile_usage(
        scope((*S3, True)), code_services={"s3", "sts", "iam", "eks", "ec2"}, trail_services=None
    )
    assert risks == []


def test_candidates_that_are_not_aws_service_resources_are_ignored():
    items = [(*o, True) for o in OTHER] + [(*o, False) for o in OTHER]
    assert reconcile_usage(scope(*items), code_services=set(), trail_services=set()) == []


def test_without_any_source_or_scope_there_is_nothing_to_compare():
    assert reconcile_usage(scope((*S3, False)), code_services=None, trail_services=None) == []
    assert reconcile_usage(None, code_services={"s3"}, trail_services={"s3"}) == []


def test_the_result_is_deterministic_and_sorted():
    items = [(*SQS, False), (*S3, False)]
    first = reconcile_usage(scope(*items), code_services={"s3", "sqs"}, trail_services=None)
    again = reconcile_usage(
        scope(*reversed(items)), code_services={"sqs", "s3"}, trail_services=None
    )
    assert first == again == sorted(first)


# --- services seen in the code and in CloudTrail ----------------------------------------------


def test_services_come_from_the_aws_sdk_call_sites_only():
    code = CodeScan(
        call_sites=[
            sdk("s3"),
            sdk("sqs", "SendMessage"),
            CallSite(kind="DB_DRIVER", target="pgx", file="main.go", line=1),
        ],
        unscannable=[],
        extension_creations=[],
        env_comparison=EnvComparison(only_in_env=[], missing_from_env=[], partial=False),
    )
    assert code_services(code) == {"s3", "sqs"}


def test_cloudtrail_services_come_from_the_event_source():
    events = [
        CloudTrailEventSummary(event_source="sqs.amazonaws.com", event_name="CreateQueue", count=1),
        CloudTrailEventSummary(event_source="s3.amazonaws.com", event_name="PutObject", count=4),
    ]
    assert trail_services(events) == {"sqs", "s3"}


# --- the scan as evidence in the report -------------------------------------------------------


def aws_scan():
    return AwsInfraScan(
        cluster=EksCluster(name="chat-prod", version=None, vpc_id="vpc-1", security_group_ids=[]),
        vpcs=[],
        subnets=[],
        route_tables=[],
        security_groups=[],
        load_balancers=[],
        role_policies=[
            RolePolicyInfo(role_arn=ROLE, actions=["s3:PutObject"], wildcard_resources=False)
        ],
        cloudtrail=[
            CloudTrailEventSummary(event_source="s3.amazonaws.com", event_name="PutObject", count=4)
        ],
        duration_seconds=0.1,
    )


def test_a_collected_scan_becomes_iam_and_cloudtrail_evidence():
    usage = build_aws_usage("COLLECTED", aws_scan())
    assert usage.status == "COLLECTED" and usage.reason is None
    role = usage.iam_roles[0]
    assert (role.role_arn, role.actions, role.wildcard_resources) == (
        ROLE,
        ["s3:PutObject"],
        False,
    )
    assert role.evidence == Evidence(kind="IAM_POLICY", locator=ROLE, verified=True)
    call = usage.cloudtrail[0]
    assert (call.event_source, call.event_name, call.count) == ("s3.amazonaws.com", "PutObject", 4)
    assert call.evidence == Evidence(
        kind="CLOUDTRAIL", locator="s3.amazonaws.com:PutObject", verified=True
    )


def test_a_scan_that_did_not_run_is_stated_with_its_reason_and_has_no_evidence():
    usage = build_aws_usage("NOT_COLLECTED", None, reason="AWS_SCAN_NOT_CONFIGURED")
    assert usage == AwsUsage(status="NOT_COLLECTED", reason="AWS_SCAN_NOT_CONFIGURED")
    failed = build_aws_usage("FAILED", None, reason="ACCESS_DENIED")
    assert failed.status == "FAILED" and failed.reason == "ACCESS_DENIED"
    assert cloud_resource_locators(usage) == set()


def test_the_evidence_check_verifies_mapping_evidence_against_the_scan_locators(tmp_path):
    usage = build_aws_usage("COLLECTED", aws_scan())
    row = MappingRow.model_validate(
        {
            "id": "object-storage-1",
            "capability": "object-storage",
            "usage": "uploads",
            "grade": "DIRECT",
            "required_proposed": True,
            "required_reason": "uploads",
            "required": None,
            "disposition": "KEEP",
            "loses": [],
            "treatment": None,
            "status": "UNVERIFIED",
            "evidence": [
                {"kind": "IAM_POLICY", "locator": ROLE},
                {"kind": "CLOUDTRAIL", "locator": "s3.amazonaws.com:PutObject"},
                {"kind": "CLOUDTRAIL", "locator": "s3.amazonaws.com:Invented"},
            ],
        }
    )
    checked = check_evidence(
        [row],
        workspace=tmp_path,
        manifest_locators=set(),
        catalog_capabilities={"object-storage"},
        cloud_resource_ids=cloud_resource_locators(usage),
    )
    assert [e.verified for e in checked[0].evidence] == [True, True, False]


# --- the report ---------------------------------------------------------------------------------


def test_old_reports_without_the_new_fields_still_read():
    assert AnalysisReport.model_fields["aws_usage"].default is None
    assert AnalysisInput.model_fields["scope"].default is None


def test_the_pipeline_puts_usage_and_the_comparison_in_the_report(repo):
    work = repo["work"]
    (work / "main.go").write_text(
        MAIN_GO.replace(
            '"github.com/jackc/pgx/v5"',
            '"github.com/jackc/pgx/v5"\n\t"github.com/aws/aws-sdk-go-v2/service/s3"',
        )
    )
    git(work, "commit", "-q", "-am", "s3")
    git(work, "push", "-q", str(repo["url"]), "HEAD:refs/heads/main")
    sha = git(work, "rev-parse", "HEAD")
    pipeline = make(repo)
    inp = AnalysisInput(
        migration_id="mig-1",
        repository=repo["url"],
        requested_commit=sha,
        manifests_dir="k8s",
        scope=scope((*S3, False), (*SQS, True), ("IAM_ROLE", "chat-api-irsa", ROLE, True)),
    ).model_dump(mode="json")
    source = pipeline.fetch_source(inp)
    code = pipeline.scan_code(inp, source)
    manifests = pipeline.scan_manifests(inp, source)
    infra = {"status": "COLLECTED", "ref": pipeline._put(aws_scan(), "mig-1", "aws-scan")}
    built = pipeline.build_requirements(inp, source, code, manifests, infra)
    pre = pipeline.precheck(inp, source, code, manifests)
    report = pipeline.check_evidence(
        inp, source, built, infra, pre, {"status": "NOT_RUN", "rows_ref": None}
    )
    full = pipeline.store.get_json(ArtifactRef.model_validate(report["report_ref"]))
    assert full["aws_usage"]["status"] == "COLLECTED"
    assert full["aws_usage"]["iam_roles"][0]["role_arn"] == ROLE
    assert full["aws_usage"]["cloudtrail"][0]["evidence"]["locator"] == "s3.amazonaws.com:PutObject"
    risks = full["unverified_risks"]
    assert any(r.startswith("SCOPE_EXCLUDED_BUT_USED:") and "chat-attachments" in r for r in risks)
    assert any(r.startswith("SCOPE_SELECTED_NO_USAGE:") and "chat-events" in r for r in risks)
    assert not any("INFRA_NOT_COLLECTED" in r for r in risks)
