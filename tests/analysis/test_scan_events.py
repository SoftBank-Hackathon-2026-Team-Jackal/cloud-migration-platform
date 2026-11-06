"""Issue #226: the analysis Activities report `analysis.scan` batches and `analysis.done`.

Count-only items with web keys, a fixed n/m per stream, the input's `migration_id`, and no source
text, `.env` values or tokens. Emitting never fails a scan.
"""

import json

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import validate_data
from cmp_scanners.aws_infra import AwsInfraScan
from cmp_worker import analysis_events
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.events import configure_emitter
from pipeline_support import AWS_KEY, SECRET, analysis_input, make
from temporalio.testing import ActivityEnvironment

MID = "migration-11111111-2222-3333-4444-555555555555"


@pytest.fixture
def emitter():
    memory = InMemoryEmitter()
    previous = configure_emitter(memory)
    yield memory
    configure_emitter(previous)


async def run_all(repo, inp):
    activities = AnalysisActivities(make(repo))
    env = ActivityEnvironment()
    source = await env.run(activities.fetch_source, inp)
    scan = {"input": inp, "source": source}
    code = await env.run(activities.scan_code, scan)
    manifests = await env.run(activities.scan_manifests, scan)
    infra = await env.run(activities.scan_aws_infra, inp)
    built = await env.run(
        activities.build_requirements,
        {**scan, "code_ref": code, "manifests_ref": manifests, "infra": infra},
    )
    return built


def by_activity(emitter):
    return {e.data["activity"]: e for e in emitter.of_type("analysis.scan")}


def keys(event):
    return {item["key"]: item["n"] for item in event.data["items"]}


@pytest.mark.asyncio
async def test_each_scan_reports_one_count_batch_and_done_follows(repo, emitter):
    inp = {**analysis_input(repo), "migration_id": MID}
    await run_all(repo, inp)
    scans = by_activity(emitter)
    assert list(scans) == [
        "fetch_source", "scan_code", "scan_manifests", "scan_aws_infra", "scan_network",
    ]  # fmt: skip
    assert {e.migration_id for e in emitter.events} == {MID}
    assert {
        a: (e.data["stream"], e.data["completed"], e.data["total"]) for a, e in scans.items()
    } == {
        "fetch_source": ("code", 1, 3),
        "scan_manifests": ("code", 2, 3),
        "scan_code": ("code", 3, 3),
        "scan_aws_infra": ("infrastructure", 1, 2),
        "scan_network": ("infrastructure", 2, 2),
    }
    assert all(e.phase == "analysis" and e.status == "running" for e in scans.values())
    source = keys(scans["fetch_source"])
    assert source["commit"] == 1 and source["files"] >= 4
    assert scans["fetch_source"].data["evidence"][0]["locator"].endswith("@" + repo["sha"][:7])
    code = keys(scans["scan_code"])
    assert code["env_keys"] == 7 and code["db_driver"] == 1
    manifests = keys(scans["scan_manifests"])
    assert manifests["manifests"] >= 1 and "services" in manifests and "statefulsets" in manifests
    # No AWS scan configured: the infrastructure slots still complete, with nothing found.
    assert scans["scan_aws_infra"].data["items"] == []
    [done] = emitter.of_type("analysis.done")
    assert done.status == "done" and done.data == {} and done.seq > scans["scan_network"].seq
    for event in emitter.events:  # the stored data is valid web data
        assert validate_data(event.type, event.data) == event.data


@pytest.mark.asyncio
async def test_events_hold_counts_not_values_or_tokens(repo, emitter):
    inp = {**analysis_input(repo), "migration_id": MID}
    await run_all(repo, inp)
    text = json.dumps([e.data for e in emitter.events])
    for value in (SECRET, AWS_KEY, "DB_PASSWORD", "package main", str(repo["tmp"])):
        assert value not in text


def test_repository_locator_drops_user_info_and_scheme():
    assert (
        analysis_events.repository_locator(
            "https://x-access-token:ghs_abc123@github.com/acme/app.git", "0123456789abcdef"
        )
        == "github.com/acme/app@0123456"
    )


def test_aws_and_network_batches_use_web_keys_and_stay_unverified():
    scan = AwsInfraScan.model_validate(
        {
            "cluster": {"name": "demo", "version": "1.30", "vpc_id": "vpc-1",
                        "security_group_ids": ["sg-1"]},
            "vpcs": [{"id": "vpc-1", "cidrs": ["10.0.0.0/16"]}],
            "subnets": [{"id": "s-1", "vpc_id": "vpc-1", "cidr": None},
                        {"id": "s-2", "vpc_id": "vpc-1", "cidr": None}],
            "route_tables": [],
            "security_groups": [{"id": "sg-1", "name": "app", "vpc_id": "vpc-1", "tags": {},
                                 "ingress": [], "egress": []}],
            "load_balancers": [{"name": "alb", "type": "application", "scheme": "internet-facing",
                                "listeners": []}],
            "role_policies": [{"role_arn": "arn:aws:iam::123456789012:role/app-irsa",
                               "actions": ["s3:GetObject"], "wildcard_resources": False}],
            "cloudtrail": [{"event_source": "s3.amazonaws.com", "event_name": "PutObject",
                            "count": 3}],
            "duration_seconds": 1.0,
        }
    )  # fmt: skip
    items, evidence = analysis_events.summarize_aws({"status": "COLLECTED"}, scan)
    assert {i["key"]: i["n"] for i in items} == {
        "resources": 6, "eks_cluster": 1, "alb": 1, "iam_role": 1, "trail_calls": 1,
    }  # fmt: skip
    assert all(not e["verified"] for e in evidence)
    assert "123456789012" not in json.dumps(evidence)
    items, _ = analysis_events.summarize_network(scan, None)
    assert {i["key"]: i["n"] for i in items} == {"vpc": 1, "subnet": 2, "security_group": 1}


@pytest.mark.asyncio
async def test_a_failed_summary_leaves_a_log_and_the_scan_still_succeeds(emitter):
    def broken():
        raise RuntimeError("boom")

    await ActivityEnvironment().run(analysis_events.emit_scan, MID, "scan_code", broken)
    assert emitter.of_type("analysis.scan") == []
    assert [log["code"] for log in emitter.logs] == ["ANALYSIS_EVENT_FAILED"]
