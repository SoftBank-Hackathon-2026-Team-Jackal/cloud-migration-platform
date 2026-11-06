"""Ticket 58: the real analysis Activities emit `analysis.finding` and `analysis.done`.

Public seam: `AnalysisActivities` run in a Temporal `ActivityEnvironment` with an
`InMemoryEmitter`; stored events are checked against the contract models.
"""

import json

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import AnalysisDoneData, AnalysisFindingData
from cmp_scanners.aws_infra import AwsInfraScan
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.analysis_events import infra_findings
from cmp_worker.events import configure_emitter
from pipeline_support import SECRET, analysis_input, make
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from test_integration import ROWS, mapping_llm

CODE_KEYS = {
    "commit", "files", "manifests", "services", "statefulsets",
    "db_driver", "env_keys", "health_endpoint", "resources",
}  # fmt: skip
INFRA_KEYS = {
    "eks_cluster", "node", "vpc", "subnet", "security_group",
    "alb", "iam_role", "nat_gateway", "ebs_gb", "trail_calls",
}  # fmt: skip
MARKERS = {"code-done", "infrastructure-done"}


@pytest.fixture
def emitter():
    stored = InMemoryEmitter()
    previous = configure_emitter(stored)
    yield stored
    configure_emitter(previous)


def aws_scan() -> AwsInfraScan:
    return AwsInfraScan.model_validate(
        {
            "cluster": {
                "name": "demo", "version": "1.30", "vpc_id": "vpc-1",
                "security_group_ids": ["sg-1"],
            },
            "vpcs": [{"id": "vpc-1", "cidrs": ["10.0.0.0/16"]}],
            "subnets": [
                {"id": f"subnet-{i}", "vpc_id": "vpc-1", "cidr": f"10.0.{i}.0/24"}
                for i in range(4)
            ],
            "route_tables": [],
            "security_groups": [
                {"id": f"sg-{i}", "name": f"g{i}", "vpc_id": "vpc-1", "tags": {},
                 "ingress": [], "egress": []}
                for i in range(3)
            ],
            "load_balancers": [
                {"name": "web", "type": "application", "scheme": "internet-facing",
                 "listeners": []},
                {"name": "tcp", "type": "network", "scheme": "internal", "listeners": []},
            ],
            "role_policies": [
                {"role_arn": "arn:aws:iam::1:role/app", "actions": ["s3:GetObject"],
                 "wildcard_resources": False},
            ],
            "cloudtrail": [
                {"event_source": "s3.amazonaws.com", "event_name": "GetObject", "count": 4},
                {"event_source": "s3.amazonaws.com", "event_name": "PutObject", "count": 2},
            ],
            "duration_seconds": 1.0,
        }
    )  # fmt: skip


def with_aws(pipeline):
    """Make `scan_aws_infra` return a collected scan without calling AWS."""

    def collected(inp):
        ref = pipeline._put(aws_scan(), inp["migration_id"], "aws-scan")
        return {"status": "COLLECTED", "ref": ref}

    pipeline.scan_aws_infra = collected
    return pipeline


async def run_all(env: ActivityEnvironment, activities: AnalysisActivities, inp: dict) -> dict:
    source = await env.run(activities.fetch_source, inp)
    scan = {"input": inp, "source": source}
    # The Workflow runs the scans in parallel; this is the web mock order.
    manifests = await env.run(activities.scan_manifests, scan)
    code = await env.run(activities.scan_code, scan)
    infra = await env.run(activities.scan_aws_infra, inp)
    built = await env.run(
        activities.build_requirements,
        {**scan, "code_ref": code, "manifests_ref": manifests, "infra": infra},
    )
    pre = await env.run(activities.precheck, {**scan, "code_ref": code, "manifests_ref": manifests})
    mapped = await env.run(
        activities.map_usage,
        {**scan, "code_ref": code, "manifests_ref": manifests, "built": built, "precheck": pre},
    )
    return await env.run(
        activities.check_evidence,
        {**scan, "built": built, "infra": infra, "precheck": pre, "mapped": mapped},
    )


def findings(emitter):
    return [e for e in emitter.events if e.type == "analysis.finding"]


@pytest.mark.asyncio
async def test_each_scan_emits_findings_that_match_the_contract_and_done_comes_last(repo, emitter):
    pipeline = with_aws(make(repo, llm_factory=mapping_llm(*ROWS)))
    await run_all(ActivityEnvironment(), AnalysisActivities(pipeline), analysis_input(repo))

    events = emitter.events
    assert {e.migration_id for e in events} == {"mig-1"}
    assert all(e.phase == "analysis" for e in events)
    stored = [AnalysisFindingData.model_validate(e.data) for e in findings(emitter)]
    by_key = {f.key: f for f in stored}
    code = {f.key for f in stored if f.stream == "code"} - MARKERS
    infra = {f.key for f in stored if f.stream == "infrastructure"} - MARKERS
    # every key the fixture has a value for, and only keys of the agreed list
    assert code == CODE_KEYS
    # node / nat_gateway / ebs_gb have no source in the scan: they are never sent
    assert infra == INFRA_KEYS - {"node", "nat_gateway", "ebs_gb"}
    assert all(f.id == f"{f.stream}-{f.key}" for f in stored if f.key not in MARKERS)
    assert by_key["commit"].detail == f"1 · {repo['sha'][:12]}"
    assert all(f.message == "" for f in stored)  # values only, no text
    assert by_key["services"].via == ["config"] and by_key["db_driver"].via == ["code"]
    assert by_key["iam_role"].via == ["iam"] and by_key["trail_calls"].via == ["trail"]
    assert by_key["subnet"].detail == "4" and by_key["alb"].detail == "1"
    assert by_key["trail_calls"].detail == "6" and by_key["eks_cluster"].detail == "1 · demo 1.30"
    # progress rises per stream and only the end markers reach 100
    for stream in ("code", "infrastructure"):
        mine = [f.progress for f in stored if f.stream == stream]
        assert mine == sorted(mine) and mine[-1] == 100
        assert all(p < 100 for p in mine[:-1])
    assert by_key["code-done"].progress == by_key["infrastructure-done"].progress == 100
    # analysis.done exactly once, after every finding, with empty data
    done = [e for e in events if e.type == "analysis.done"]
    assert len(done) == 1 and events[-1] is done[0]
    assert AnalysisDoneData.model_validate(done[0].data).model_dump() == {}
    assert done[0].status == "done" and done[0].progress == 100


@pytest.mark.asyncio
async def test_no_secret_value_or_env_name_reaches_event_data(repo, emitter):
    pipeline = with_aws(make(repo, llm_factory=mapping_llm(*ROWS)))
    await run_all(ActivityEnvironment(), AnalysisActivities(pipeline), analysis_input(repo))
    dumped = json.dumps([e.data for e in emitter.events])
    assert SECRET not in dumped
    for name in ("DB_PASSWORD", "DB_HOST", "APP_PORT"):
        assert name not in dumped
    env_keys = next(f for f in findings(emitter) if f.data["key"] == "env_keys")
    assert env_keys.data["detail"] == "7"  # the count only


@pytest.mark.asyncio
async def test_repeating_an_activity_in_the_same_attempt_stores_each_finding_once(repo, emitter):
    activities = AnalysisActivities(make(repo))
    env = ActivityEnvironment()
    inp = analysis_input(repo)
    await env.run(activities.fetch_source, inp)
    first = len(emitter.events)
    await env.run(activities.fetch_source, inp)
    assert first == 2 and len(emitter.events) == first


@pytest.mark.asyncio
async def test_an_uncollected_aws_scan_sends_no_infrastructure_keys_but_still_finishes(
    repo, emitter
):
    pipeline = make(repo, llm_factory=mapping_llm(*ROWS))  # no AWS clients: NOT_COLLECTED
    await run_all(ActivityEnvironment(), AnalysisActivities(pipeline), analysis_input(repo))
    infra = [f.data for f in findings(emitter) if f.data["stream"] == "infrastructure"]
    assert [f["key"] for f in infra] == ["infrastructure-done"]
    assert infra[0]["progress"] == 100
    assert [e.type for e in emitter.events].count("analysis.done") == 1


@pytest.mark.asyncio
async def test_a_failed_step_emits_nothing_and_no_done(repo, emitter):
    activities = AnalysisActivities(make(repo))
    env = ActivityEnvironment()
    inp = analysis_input(repo)
    source = await env.run(activities.fetch_source, inp)
    before = len(emitter.events)
    broken = {**source, "workspace": str(repo["tmp"] / "missing")}
    with pytest.raises(ApplicationError):
        await env.run(activities.scan_manifests, {"input": inp, "source": broken})
    with pytest.raises(ApplicationError):
        await env.run(
            activities.check_evidence,
            {"input": inp, "source": source, "built": {"manifests_ref": None}, "infra": {},
             "precheck": {}, "mapped": {}},
        )  # fmt: skip
    assert len(emitter.events) == before
    assert "analysis.done" not in [e.type for e in emitter.events]


def test_infrastructure_findings_follow_the_web_order_and_skip_missing_values():
    items = infra_findings(aws_scan())
    assert [i[0] for i in items] == [
        "eks_cluster", "vpc", "subnet", "security_group", "alb", "iam_role", "trail_calls",
    ]  # fmt: skip
    empty = aws_scan().model_copy(
        update={"load_balancers": [], "role_policies": [], "cloudtrail": []}
    )
    assert [i[0] for i in infra_findings(empty)] == [
        "eks_cluster", "vpc", "subnet", "security_group",
    ]  # fmt: skip
