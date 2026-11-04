"""Ticket 08: execute_aws_plan Activity reloads the approved Plan from the business DB."""

import pytest
from cmp_api.approvals import publish_plan_revision
from cmp_api.db import Base
from cmp_providers.plans import manifest_digest
from cmp_worker.activities.aws_infra import AwsInfraActivities
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment
from test_aws_apply import FakeRunner, artifacts


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
        "provider": "AWS",
        "scope": manifest.scope,
        "manifest_digest": digest or manifest_digest(manifest),
    }


async def test_activity_applies_stored_plan_and_heartbeats(db, tmp_path):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    runner = FakeRunner()
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    activities = AwsInfraActivities(
        db, artifact_root=tmp_path, aws_profile="cmp-aws-prod", runner=runner
    )
    result = await env.run(activities.execute_aws_plan, claim(manifest))
    assert result["status"] == "SUCCEEDED"
    assert beats  # at least the start heartbeat
    assert any(args[:2] == ["terraform", "apply"] for args in runner.commands)


async def test_activity_refuses_when_claim_digest_differs_from_db(db, tmp_path):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    runner = FakeRunner()
    activities = AwsInfraActivities(
        db, artifact_root=tmp_path, aws_profile="cmp-aws-prod", runner=runner
    )
    result = await ActivityEnvironment().run(
        activities.execute_aws_plan, claim(manifest, digest="f" * 64)
    )
    assert (result["status"], result["code"]) == ("FAILED", "MANIFEST_DIGEST_MISMATCH")
    assert runner.commands == []


async def test_activity_without_artifact_root_is_failed(db, tmp_path):
    manifest, preview, _ = artifacts(tmp_path)
    with db() as s:
        publish_plan_revision(s, manifest, preview)
    activities = AwsInfraActivities(db, artifact_root=None, aws_profile="cmp-aws-prod")
    result = await ActivityEnvironment().run(activities.execute_aws_plan, claim(manifest))
    assert (result["status"], result["code"]) == ("FAILED", "ARTIFACT_ROOT_NOT_CONFIGURED")
