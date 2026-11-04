"""Ticket 10: execute_gcp_plan Activity and GCP dispatch."""

import pytest
from cmp_api.approvals import publish_plan_revision
from cmp_api.db import Base
from cmp_contracts.execution import execution_steps
from cmp_providers.plans import manifest_digest
from cmp_worker.activities.gcp_infra import GcpInfraActivities
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment
from test_gcp_apply import ADC, FakeRunner, artifacts


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def claim(manifest, digest=None):
    return {
        "approval_id": "approval-1",
        "plan_id": manifest.plan.plan_id,
        "revision": manifest.plan.revision,
        "provider": "GCP",
        "scope": manifest.scope,
        "manifest_digest": digest or manifest_digest(manifest),
        "resource_refs": {},
    }


def activities(db, tmp_path, runner, **override):
    options = {
        "artifact_root": tmp_path,
        "aws_profile": "cmp-aws-prod",
        "gcloud_configuration": "cmp-gcp-prod",
        "adc_email": ADC,
        "runner": runner,
        "adc_lookup": lambda env: ADC,
    }
    options.update(override)
    return GcpInfraActivities(db, **options)


def test_gcp_dispatch_order():
    assert execution_steps("GCP", "INFRASTRUCTURE") == ("execute_gcp_plan",)
    assert execution_steps("GCP", "WORKLOADS") == ("execute_gcp_workloads",)
    assert execution_steps("GCP", "FULL") == ("execute_gcp_plan", "execute_gcp_workloads")


async def test_activity_applies_stored_gcp_plan(db, tmp_path):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    runner = FakeRunner()
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    result = await env.run(activities(db, tmp_path, runner).execute_gcp_plan, claim(manifest))
    assert result["status"] == "SUCCEEDED"
    assert beats
    assert any(args[:2] == ["terraform", "apply"] for args in runner.commands)


async def test_activity_refuses_digest_mismatch(db, tmp_path):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    runner = FakeRunner()
    result = await ActivityEnvironment().run(
        activities(db, tmp_path, runner).execute_gcp_plan, claim(manifest, "f" * 64)
    )
    assert result["code"] == "MANIFEST_DIGEST_MISMATCH"
    assert runner.commands == []


async def test_activity_requires_configuration(db, tmp_path):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    acts = activities(db, tmp_path, FakeRunner(), artifact_root=None)
    result = await ActivityEnvironment().run(acts.execute_gcp_plan, claim(manifest))
    assert result["code"] == "ARTIFACT_ROOT_NOT_CONFIGURED"
