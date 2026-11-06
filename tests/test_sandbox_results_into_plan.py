"""Ticket 55 (#190): the Sandbox results (EnvPlan, cleanup status, verified target) reach the Plan.

Seams: the receiving adapter `cmp_worker.sandbox_handoff.sandbox_outcome` (pure), the real
`sbx_plan_assemble` and `register_planning_input` Activities over one content-addressed store
directory (the demo shares it), the real ticket 18 `PlanPublisher` (ON_PREM, SQLite business DB,
real Helm render), the ticket 12 job builder and `job_env_secrets` with `InMemoryEnvFileStore`.
The producer formats (analysis `assembly.env_plan_ref`, the Sandbox result dict) are used as they
are; fields the producer does not write yet are only read when present.
"""

import json
from pathlib import Path

import pytest
import test_plan_publish
from cmp_api.agent_registry import AgentJob
from cmp_api.onprem_rollout import LocalArtifactStore as PlatformStore
from cmp_api.onprem_rollout import build_rollout_job, dispatch, job_env_secrets
from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.verification import canonical_requirements
from cmp_providers.env_file import InMemoryEnvFileStore
from cmp_providers.plans import approval_issues
from cmp_testsuite import Suite, dump_bundle
from cmp_worker.activities.planning_register import PlanningRegisterActivities
from cmp_worker.activities.sandbox_loop import SandboxLoopActivities
from cmp_worker.artifact_store import ArtifactRef, LocalArtifactStore
from cmp_worker.build_wiring import upload_suite
from cmp_worker.planning_handoff import HandoffError
from cmp_worker.sandbox_handoff import sandbox_outcome
from onprem_rollout_support import register_agent
from sandbox.test_deploy import FakeRunner, make_service
from sandbox.test_handoff import evidence
from sandbox.test_handoff_activity import Archives
from sandbox.test_publish import case
from test_cloud_workload_plan import approve
from test_plan_publish import DB_IMAGE, publisher, stored

db = test_plan_publish.db
platform_registry = test_plan_publish.platform_registry  # ticket 29: autouse here too

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"
ENV_TEXT = b"APP_MODE=prod\nAPI_TOKEN=tok-SECRET-55\nAWS_SECRET_ACCESS_KEY=aws-SECRET-55\n"
TARGET = {"id": "target-onprem-k3s-basic", "version": "1"}
ONPREM = {
    "provider": "ON_PREM",
    "environment_id": "onprem-prod-001",
    "agent_id": "agent-a",
    "cluster_uid": "cluster-a",
    "namespace": "cmp-prod-demo",
    "storage_class": "local-path",
    "database_image": DB_IMAGE,
}


# --- the receiving adapter -------------------------------------------------------------------


def test_a_returned_pass_without_cleanup_fields_was_cleaned_and_names_no_target():
    # The Sandbox Workflow raises when the teardown of a passing run fails, so a returned
    # result has been torn down (workflows/sandbox.py `finally`).
    outcome = sandbox_outcome({"status": "PASSED"})
    assert (outcome.cleanup_status, outcome.verified_target_ref) == ("CLEANED", None)


def test_cleanup_and_target_fields_are_read_when_the_sandbox_writes_them():
    assert sandbox_outcome({"cleanup_failed": True}).cleanup_status == "FAILED"
    assert sandbox_outcome({"cleanup_status": "PENDING"}).cleanup_status == "PENDING"
    # An explicit status wins over the flag.
    assert sandbox_outcome(
        {"cleanup_status": "FAILED", "cleanup_failed": False}
    ).cleanup_status == ("FAILED")
    outcome = sandbox_outcome({"target_selection_ref": TARGET})
    assert outcome.verified_target_ref.model_dump() == TARGET


@pytest.mark.parametrize(
    "result",
    [
        {"cleanup_status": "GONE"},
        {"cleanup_failed": "yes"},
        {"target_selection_ref": {"id": "x"}},
        {"target_selection_ref": "target-1"},
    ],
)
def test_a_malformed_sandbox_field_is_refused_with_a_code(result):
    with pytest.raises(HandoffError) as caught:
        sandbox_outcome(result)
    assert caught.value.code == "SANDBOX_RESULT_INVALID"


# --- sandbox result -> registration -> Plan -> job ------------------------------------------


@pytest.fixture
def env_store():
    return InMemoryEnvFileStore()


def world(tmp_path, env_store, *, env_plan=True, **sandbox_extra):
    """Store the analysis and Sandbox outputs the way their producers do, ON_PREM target."""
    args, _ = evidence()
    store = LocalArtifactStore(tmp_path / "artifacts")
    raw = json.loads((FIXTURES / "analysis-report-eks-k3s.json").read_text(encoding="utf-8"))
    report = AnalysisReport.model_validate(raw["report"])
    requirements = RequirementsV2.model_validate_json(
        (FIXTURES / "requirements-eks-k3s.json").read_bytes()
    )
    req_ref = store.put(
        canonical_requirements(requirements)[0], ref_id="requirements", kind="requirements"
    )
    report_ref = store.put_json(report, ref_id="analysis", kind="analysis-report")
    builds_ref = store.put(
        json.dumps([b.model_dump(mode="json") for b in args["builds"]]).encode(),
        ref_id="build-attempts", kind="build-attempts",
    )  # fmt: skip
    suite_ref = store.put(
        dump_bundle(Suite(name="s", cases=[case("pg", ["postgres"])])),
        ref_id="suite",
        kind="test-suite",
    )
    env_ref = env_store.put(ENV_TEXT).model_dump(mode="json")
    assembly = {"env_exceptions": raw["env_exceptions"]}
    if env_plan:  # ENV-05 producer shape: analysis stores the EnvPlan, assembly holds its ref
        plan = {
            "env_file_ref": env_ref,
            "entries": [
                {"key": "APP_MODE", "disposition": "PASS"},
                {"key": "API_TOKEN", "disposition": "PASS"},
                {
                    "key": "AWS_SECRET_ACCESS_KEY",
                    "disposition": "EXCLUDED",
                    "reason": "SOURCE_ONLY_CREDENTIAL",
                },
            ],
        }
        assembly["env_plan_ref"] = store.put_json(
            plan, ref_id="env-plan", kind="env-plan"
        ).model_dump(mode="json")
    assembly_ref = store.put_json(assembly, ref_id="assembly", kind="assembly")
    archives = Archives()
    activities = SandboxLoopActivities(
        service=make_service(FakeRunner()), store=store, workspace_root=tmp_path / "w",
        build=lambda *_: None, suite_runner=lambda *_: None,
        suite_uploader=lambda bundle: upload_suite(archives, "cmp-sources", bundle),
    )  # fmt: skip
    settings = {
        "target_selection": {
            "ref": TARGET,
            "provider": "ON_PREM",
            "location": None,
            "access_ref": {"id": "agent-a", "version": "cluster-a"},
            "capabilities_ref": {"id": "profile-onprem-k3s-basic", "version": "1"},
        },
        "target": ONPREM,
    }
    payload = {
        "migration_id": report.migration_id,
        # The parent copies the start request's reference in (ENV-02).
        "settings": {**settings, "env_file_ref": env_ref},
        "suite_ref": suite_ref.model_dump(mode="json"),
        "analysis": {"assembly_ref": assembly_ref.model_dump(mode="json")},
        "sandbox": {
            "status": "PASSED",
            "mode": "integrated",
            "attempts": [{"attempt": 1, "passed": True}],
            "published": {
                "status": "PUBLISHED",
                "report_ref": report_ref.model_dump(mode="json"),
                "requirements_ref": req_ref.model_dump(mode="json"),
            },
            "builds_ref": builds_ref.model_dump(mode="json"),
            **sandbox_extra,
        },
    }
    return activities, payload, store


def registration(store, assembled) -> dict:
    return json.loads(store.get(ArtifactRef.model_validate(assembled["registration_ref"])))


async def hand_off_and_publish(tmp_path, db, env_store, **kw):
    activities, payload, store = world(tmp_path, env_store, **kw)
    assembled = await activities.sbx_plan_assemble(payload)
    assert assembled["status"] == "READY", assembled
    with db() as session:
        credential = register_agent(session)
    platform = PlatformStore(tmp_path / "artifacts")  # same directory, content-addressed
    registered = await PlanningRegisterActivities(db, platform).register_planning_input(
        {"migration_id": payload["migration_id"], "registration_ref": assembled["registration_ref"]}
    )
    assert registered["status"] == "REGISTERED", registered
    published = publisher(db, platform, tmp_path, env_store=env_store).publish(
        payload["migration_id"]
    )
    return published, platform, store, assembled, credential


async def test_the_sandbox_env_plan_becomes_the_production_env_secret_of_the_onprem_job(
    tmp_path, db, env_store
):
    published, platform, store, assembled, _ = await hand_off_and_publish(tmp_path, db, env_store)
    assert published["status"] == "PUBLISHED" and published["provider"] == "ON_PREM", published
    assert registration(store, assembled)["env_plan"]["entries"][1]["key"] == "API_TOKEN"
    manifest, preview = stored(db, published)
    assert approval_issues(manifest, preview) == []
    values = json.loads(platform.read_raw(manifest.artifacts["application_values"].sha256))
    sources = {item["name"]: item["source"] for item in values["env"]}
    assert sources["API_TOKEN"] == {"kind": "SECRET", "name": "app-env", "key": "API_TOKEN"}
    assert "AWS_SECRET_ACCESS_KEY" not in sources
    claim = approve(db, published)
    with db() as session:
        job, owner = build_rollout_job(session, claim, platform)
        dispatch(session, job, owner)
    env_secret = next(s for s in job.application.env_secrets if s.name == "app-env")
    assert env_secret.keys == ["API_TOKEN", "APP_MODE"]
    with db() as session:
        agent_job = session.get(AgentJob, job.job_id)
        agent_job.status = "RUNNING"
        session.commit()
        secrets = job_env_secrets(session, agent_job, platform, env_store)
    data = next(s for s in secrets if s.name == "app-env").reveal()
    assert data == {"API_TOKEN": "tok-SECRET-55", "APP_MODE": "prod"}
    # No `.env` value in anything the platform wrote.
    blobs = [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()]
    assert all(b"tok-SECRET-55" not in blob and b"aws-SECRET-55" not in blob for blob in blobs)


async def test_a_failed_sandbox_cleanup_is_published_as_a_decision_that_blocks_approval(
    tmp_path, db, env_store
):
    published, _, store, assembled, _ = await hand_off_and_publish(
        tmp_path, db, env_store, cleanup_failed=True
    )
    assert published["status"] == "PUBLISHED", published
    assert registration(store, assembled)["planning_request"]["sandbox"]["cleanup_status"] == (
        "FAILED"
    )
    manifest, preview = stored(db, published)
    assert [(i.code, i.path) for i in preview.pending_decisions] == [
        ("CLEANUP_POLICY_REQUIRED", "sandbox.cleanup_status")
    ]
    assert {i.code for i in approval_issues(manifest, preview)} == {"PENDING_DECISION"}


async def test_a_cleaned_sandbox_keeps_the_plan_free_of_cleanup_decisions(tmp_path, db, env_store):
    published, _, store, assembled, _ = await hand_off_and_publish(
        tmp_path, db, env_store, env_plan=False
    )
    assert registration(store, assembled)["planning_request"]["sandbox"]["cleanup_status"] == (
        "CLEANED"
    )
    _, preview = stored(db, published)
    assert [i.code for i in preview.pending_decisions] == []


async def test_the_target_the_sandbox_recorded_must_be_the_planning_target(tmp_path, env_store):
    activities, payload, _ = world(
        tmp_path, env_store, target_selection_ref={"id": "target-other", "version": "1"}
    )
    assert await activities.sbx_plan_assemble(payload) == {
        "status": "REFUSED",
        "code": "TARGET_SELECTION_CHANGED",
    }
    payload["sandbox"]["target_selection_ref"] = TARGET
    assembled = await activities.sbx_plan_assemble(payload)
    assert assembled["status"] == "READY"


async def test_a_malformed_sandbox_result_is_refused_before_planning(tmp_path, env_store):
    activities, payload, _ = world(tmp_path, env_store, cleanup_status="GONE")
    assert await activities.sbx_plan_assemble(payload) == {
        "status": "REFUSED",
        "code": "SANDBOX_RESULT_INVALID",
    }
