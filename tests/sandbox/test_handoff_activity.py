"""Ticket 15: the hand-off Activities (assembly from the store, registration into the DB)."""

import json
from pathlib import Path

import pytest
from cmp_api.db import Base
from cmp_api.onprem_rollout import LocalArtifactStore as PlatformStore
from cmp_api.plan_inputs import load_planning_input
from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.verification import canonical_requirements
from cmp_testsuite import Suite, dump_bundle
from cmp_worker.activities.planning_register import PlanningRegisterActivities
from cmp_worker.activities.sandbox_loop import SandboxLoopActivities
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.build_wiring import upload_suite
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .test_deploy import FakeRunner, make_service
from .test_handoff import DIGEST, evidence
from .test_publish import case

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analysis"


class Archives:
    def __init__(self):
        self.objects = {}

    def exists(self, bucket, key):
        return (bucket, key) in self.objects

    def put(self, bucket, key, data):
        self.objects[(bucket, key)] = data


def world(tmp_path, *, with_builds=True, uploader=True):
    args, _ = evidence()
    store = LocalArtifactStore(tmp_path / "store")
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
    assembly_ref = store.put_json(
        {"env_exceptions": raw["env_exceptions"]}, ref_id="assembly", kind="assembly"
    )
    archives = Archives()
    activities = SandboxLoopActivities(
        service=make_service(FakeRunner()), store=store, workspace_root=tmp_path / "w",
        build=lambda *_: None, suite_runner=lambda *_: None,
        suite_uploader=(lambda bundle: upload_suite(archives, "cmp-sources", bundle)) if uploader else None,
    )  # fmt: skip
    payload = {
        "migration_id": report.migration_id,
        "settings": args["settings"].model_dump(mode="json"),
        "suite_ref": suite_ref.model_dump(mode="json"),
        "analysis": {"assembly_ref": assembly_ref.model_dump(mode="json")},
        "sandbox": {
            "status": "PASSED",
            "attempts": [{"attempt": 1, "passed": True}],
            "published": {
                "status": "PUBLISHED",
                "report_ref": report_ref.model_dump(mode="json"),
                "requirements_ref": req_ref.model_dump(mode="json"),
            },
            **({"builds_ref": builds_ref.model_dump(mode="json")} if with_builds else {}),
        },
    }  # fmt: skip
    return activities, payload, store, archives


@pytest.mark.asyncio
async def test_the_assembly_stores_a_registration_and_uploads_the_suite_once(tmp_path):
    activities, payload, store, archives = world(tmp_path)
    out = await activities.sbx_plan_assemble(payload)
    assert out["status"] == "READY"
    stored = json.loads(
        store.get(
            __import__(
                "cmp_worker.artifact_store", fromlist=["ArtifactRef"]
            ).ArtifactRef.model_validate(out["registration_ref"])
        )
    )
    assert stored["planning_request"]["builds"][0]["image_digest"] == DIGEST
    assert stored["planning_request"]["sandbox"]["test_suite_ref"]["uri"].startswith(
        "s3://cmp-sources/tests/"
    )
    assert len(archives.objects) == 1
    assert "requirements_json" not in json.dumps(stored) or stored.get("requirements_json") is None


@pytest.mark.asyncio
async def test_without_build_evidence_the_assembly_refuses_with_a_code(tmp_path):
    activities, payload, _, _ = world(tmp_path, with_builds=False)
    assert await activities.sbx_plan_assemble(payload) == {
        "status": "REFUSED", "code": "BUILD_EVIDENCE_MISSING",
    }  # fmt: skip


@pytest.mark.asyncio
async def test_without_a_suite_uploader_the_assembly_refuses(tmp_path):
    activities, payload, _, _ = world(tmp_path, uploader=False)
    out = await activities.sbx_plan_assemble(payload)
    assert out["code"] == "TEST_SUITE_UPLOAD_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_a_run_that_did_not_pass_is_not_handed_over(tmp_path):
    activities, payload, _, _ = world(tmp_path)
    payload["sandbox"]["status"] = "NEEDS_REVIEW"
    assert (await activities.sbx_plan_assemble(payload))["code"] == "SANDBOX_NOT_PASSED"


# ------------------------------------------------------------------ registration


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_registration_makes_the_input_readable_for_planning(tmp_path, db):
    from test_plan_publish import MIGRATION, registration

    reg, body = registration()
    platform = PlatformStore(tmp_path / "platform")
    content = body["requirements_json"].encode()
    sha = reg.planning_request.analysis.requirements_ref.sha256
    platform.write_raw(sha, content)
    doc = platform.put(
        reg.model_dump_json(exclude={"requirements_json"}).encode(),
        ref_id="planning-input",
        kind="planning-input",
    )
    activities = PlanningRegisterActivities(db, platform)
    out = await activities.register_planning_input(
        {"migration_id": MIGRATION, "registration_ref": doc}
    )
    assert out["status"] == "REGISTERED" and len(out["input_digest"]) == 64
    with db() as session:
        loaded = load_planning_input(session, MIGRATION)
    assert loaded.planning_request.migration_id == reg.planning_request.migration_id


@pytest.mark.asyncio
async def test_a_wrong_migration_or_missing_document_is_refused_with_a_code(tmp_path, db):
    from test_plan_publish import registration

    reg, body = registration()
    platform = PlatformStore(tmp_path / "platform")
    platform.write_raw(
        reg.planning_request.analysis.requirements_ref.sha256, body["requirements_json"].encode()
    )
    doc = platform.put(
        reg.model_dump_json(exclude={"requirements_json"}).encode(),
        ref_id="p",
        kind="planning-input",
    )
    activities = PlanningRegisterActivities(db, platform)
    wrong = await activities.register_planning_input(
        {"migration_id": "other", "registration_ref": doc}
    )
    assert wrong == {"status": "REFUSED", "code": "MIGRATION_MISMATCH"}
    missing = await activities.register_planning_input(
        {"migration_id": "migration-1", "registration_ref": {**doc, "sha256": "0" * 64}}
    )
    assert missing["code"] == "REGISTRATION_UNAVAILABLE"
    assert (await PlanningRegisterActivities(db, None).register_planning_input({}))[
        "status"
    ] == "SCAFFOLD"
