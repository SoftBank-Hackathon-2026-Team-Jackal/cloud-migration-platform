import hashlib
import json
from pathlib import Path

import pytest
from cmp_contracts.analysis import AnalysisReport, EnvExceptions, Evidence, MappingRow, ReviewRecord
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.verification import SandboxVerification, canonical_requirements, project_planning
from cmp_worker.provisioning_v2 import prepare_plan_v2
from pydantic import ValidationError


def test_mapping_accepts_unreviewed_proposal_and_rejects_invalid_fields():
    row = {
        "capability": "postgres",
        "usage": "queries",
        "grade": "DIRECT",
        "required_proposed": True,
        "required_reason": "Not yet assessed",
        "required": None,
        "disposition": "MIGRATE",
        "loses": [],
        "treatment": None,
        "status": "UNVERIFIED",
        "evidence": [{"kind": "CODE", "locator": "app.py:12"}],
    }
    parsed = MappingRow.model_validate(row)
    assert parsed.evidence == [Evidence(kind="CODE", locator="app.py:12")]
    assert MappingRow.model_validate_json(parsed.model_dump_json()) == parsed

    for invalid in (
        {**row, "grade": "direct"},
        {**row, "required_proposed": None},
        {**row, "required_proposed": "true"},
        {**row, "secret_value": "fake"},
    ):
        with pytest.raises(ValidationError):
            MappingRow.model_validate(invalid)


def test_review_and_env_exceptions_reject_unknown_secret_fields():
    record = {
        "analysis_ref": {"id": "analysis", "version": "1"},
        "required": {"postgres": True},
        "operational": [],
        "approvals": {"cache": "APPROVED"},
        "actor": "reviewer",
        "decided_at": "2026-11-04T00:00:00Z",
    }
    reviewed = ReviewRecord.model_validate_json(json.dumps(record))
    assert ReviewRecord.model_validate_json(reviewed.model_dump_json()) == reviewed
    with pytest.raises(ValidationError):
        EnvExceptions.model_validate(
            {"excluded": [], "replaced": {}, "missing": [], "secret_value": "fake"}
        )


def test_requirements_serialization_is_stable_and_valid_v2():
    requirements = RequirementsV2.model_validate(
        {
            "contract_version": "2",
            "resources": [
                {
                    "id": "app",
                    "kind": "APPLICATION",
                    "replicas": 1,
                    "cpu_millicores": 100,
                    "memory_mib": 128,
                    "port": 8080,
                    "health_path": "/health",
                }
            ],
        }
    )
    data, digest = canonical_requirements(requirements)
    assert (data, digest) == canonical_requirements(requirements.model_copy(deep=True))
    assert digest == hashlib.sha256(data).hexdigest()
    assert RequirementsV2.model_validate_json(data) == requirements


def test_eks_to_k3s_result_projects_to_planning():
    requirements = RequirementsV2.model_validate_json(
        (Path(__file__).parent.parent / "fixtures/analysis/requirements-eks-k3s.json").read_bytes()
    )
    content, sha = canonical_requirements(requirements)
    ref = lambda name: {"id": name, "version": "1"}
    requirements_ref = {**ref("requirements"), "sha256": sha}
    archive_sha = "a" * 64
    source = {"uri": f"s3://source/{archive_sha}.tar.gz", "sha256": archive_sha}
    suite_sha = "d" * 64
    suite = {"uri": f"s3://tests/{suite_sha}.tar.gz", "sha256": suite_sha}
    digest = "sha256:" + "b" * 64
    report = AnalysisReport.model_validate(
        {
            "migration_id": "migration-1",
            "ref": ref("analysis"),
            "base_commit": "abc123",
            "commit_verified": True,
            "usage_profile": {"usages": ["postgres"]},
            "mapping": [],
            "precheck": {"findings": []},
            "external_entries": [],
            "source_cidrs": [],
            "source_db_images": {},
            "unmapped_items": [],
            "unverified_risks": [],
        }
    )
    sandbox = SandboxVerification.model_validate(
        {
            "migration_id": "migration-1",
            "ref": ref("sandbox"),
            "analysis_ref": ref("analysis"),
            "target_selection_ref": ref("target"),
            "status": "PASSED",
            "validated_workloads": [
                {
                    "resource_id": "app",
                    "build_ref": ref("build"),
                    "image_digest": digest,
                    "platform": "linux/amd64",
                    "source_sha256": archive_sha,
                }
            ],
            "requirements_ref": requirements_ref,
            "report_ref": {**ref("report"), "sha256": "c" * 64},
            "test_suite_ref": suite,
            "attempts": [],
            "cleanup_status": "CLEANED",
        }
    )
    assert AnalysisReport.model_validate_json(report.model_dump_json()) == report
    assert SandboxVerification.model_validate_json(sandbox.model_dump_json()) == sandbox
    request = project_planning(
        report=report,
        sandbox=sandbox,
        requirements_ref=requirements_ref,
        source_refs={"app": source},
        builds=[
            {
                "resource_id": "app",
                "migration_id": "migration-1",
                "attempt_id": "attempt-1",
                "result_ref": ref("build"),
                "status": "SUCCEEDED",
                "source_ref": source,
                "image_uri": f"registry.example/app@{digest}",
                "image_digest": digest,
                "platform": "linux/amd64",
            }
        ],
        target_selection={
            "ref": ref("target"),
            "provider": "ON_PREM",
            "location": None,
            "access_ref": ref("cluster"),
            "capabilities_ref": ref("caps"),
        },
        environment={
            "env_file_ref": {**ref("env"), "sha256": "e" * 64},
            "mapping_ref": {**ref("mapping"), "sha256": "f" * 64},
        },
        policy={"template_version": "v2"},
    )
    result = prepare_plan_v2(request.model_dump(mode="json"), requirements_bytes=content)
    assert result.spec.images[0].image_digest == digest
    assert result.spec.requirements == requirements
    assert request.analysis.requirements_ref == request.sandbox.requirements_ref
    wrong_target = request.target_selection.model_dump(mode="json")
    wrong_target["ref"] = ref("different-target")
    with pytest.raises(ValueError):
        project_planning(
            report=report,
            sandbox=sandbox,
            requirements_ref=requirements_ref,
            source_refs=request.analysis.model_dump(mode="json")["source_refs"],
            builds=[
                {
                    **build.model_dump(mode="json", exclude={"platforms"}),
                    "migration_id": "migration-1",
                    "attempt_id": "attempt-1",
                    "platform": build.platforms[0],
                }
                for build in request.builds
            ],
            target_selection=wrong_target,
            environment=request.environment.model_dump(mode="json"),
            policy=request.policy.model_dump(mode="json"),
        )
    with pytest.raises(ValueError):
        project_planning(
            report=report,
            sandbox=sandbox.model_copy(update={"analysis_ref": {"id": "old", "version": "1"}}),
            requirements_ref=requirements_ref,
            source_refs={},
            builds=[],
            target_selection={},
            environment={},
            policy={},
        )
    mismatched = request.model_copy(deep=True)
    mismatched.analysis.requirements_ref.sha256 = "0" * 64
    with pytest.raises(ValueError):
        prepare_plan_v2(mismatched.model_dump(mode="json"), requirements_bytes=content)
    with pytest.raises(ValueError):
        project_planning(
            report=report,
            sandbox=sandbox.model_copy(update={"status": "NEEDS_REVIEW"}),
            requirements_ref=requirements_ref,
            source_refs={},
            builds=[],
            target_selection={},
            environment={},
            policy={},
        )
