import hashlib
import json
from pathlib import Path

import pytest
from cmp_contracts.analysis import AnalysisReport, EnvExceptions, MappingRow, ReviewRecord
from cmp_contracts.provisioning import ContentRef
from cmp_contracts.provisioning_v2 import (
    EnvironmentInputV2,
    PlanningPolicyV2,
    RequirementsV2,
    S3ArchiveRef,
    TargetSelectionV2,
)
from cmp_contracts.verification import (
    BuildAttempt,
    SandboxVerification,
    canonical_requirements,
    project_planning,
)
from cmp_worker.provisioning_v2 import prepare_plan_v2
from pydantic import ValidationError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analysis"


@pytest.fixture
def analysis_fixture():
    data = json.loads((FIXTURES / "analysis-report-eks-k3s.json").read_text(encoding="utf-8"))
    return AnalysisReport.model_validate(data["report"]), EnvExceptions.model_validate(
        data["env_exceptions"]
    )


def test_report_and_environment_roundtrip_and_reject_invalid_fields(analysis_fixture):
    report, env = analysis_fixture
    assert AnalysisReport.model_validate_json(report.model_dump_json()) == report
    assert EnvExceptions.model_validate_json(env.model_dump_json()) == env
    assert (report.mapping[0].grade, report.mapping[0].treatment) == ("PARTIAL", "APPROVAL")
    assert report.mapping[1].disposition == "KEEP"
    assert report.external_entries[0].target_resource_id == "app"
    assert env.replaced == {"PUBLIC_URL": "https://example.test"}

    row = report.mapping[0].model_dump()
    for invalid in (
        {**row, "grade": "partial"},
        {**row, "required_proposed": None},
        {**row, "required_proposed": "true"},
        {**row, "secret_value": "fake"},
    ):
        with pytest.raises(ValidationError):
            MappingRow.model_validate(invalid)
    with pytest.raises(ValidationError):
        EnvExceptions.model_validate({**env.model_dump(), "secret_value": "fake"})


def test_mapping_row_ids_are_unique_within_report(analysis_fixture):
    report, _ = analysis_fixture
    payload = report.model_dump(mode="json")
    payload["mapping"][1]["capability"] = payload["mapping"][0]["capability"]
    assert len(AnalysisReport.model_validate(payload).mapping) == 2
    payload["mapping"][1]["id"] = payload["mapping"][0]["id"]
    with pytest.raises(ValidationError):
        AnalysisReport.model_validate(payload)


def test_review_records_distinct_rows_with_same_capability(analysis_fixture):
    report, _ = analysis_fixture
    data = report.model_dump(mode="json")
    data["mapping"][1]["capability"] = data["mapping"][0]["capability"]
    report = AnalysisReport.model_validate(data)
    first, second = report.mapping
    record = {
        "analysis_ref": report.ref.model_dump(),
        "required": {first.id: False, second.id: True},
        "operational": [],
        "approvals": {first.id: "APPROVED"},
        "actor": "reviewer",
        "decided_at": "2026-11-04T00:00:00Z",
    }
    reviewed = ReviewRecord.model_validate_json(json.dumps(record))
    assert reviewed.required == {"postgres-advisory-lock": False, "s3-object-read": True}
    assert first.capability == second.capability
    assert ReviewRecord.model_validate_json(reviewed.model_dump_json()) == reviewed


def test_requirements_serialization_is_stable_and_valid_v2():
    requirements = RequirementsV2.model_validate_json(
        (FIXTURES / "requirements-eks-k3s.json").read_bytes()
    )
    data, digest = canonical_requirements(requirements)
    assert (data, digest) == canonical_requirements(RequirementsV2.model_validate_json(data))
    assert digest == hashlib.sha256(data).hexdigest()
    assert RequirementsV2.model_validate_json(data) == requirements


@pytest.fixture
def planning_inputs(analysis_fixture):
    report, env = analysis_fixture
    requirements = RequirementsV2.model_validate_json(
        (FIXTURES / "requirements-eks-k3s.json").read_bytes()
    )
    content, sha = canonical_requirements(requirements)

    def ref(name):
        return {"id": name, "version": "1"}

    requirements_ref = ContentRef.model_validate({**ref("requirements"), "sha256": sha})
    archive_sha = "a" * 64
    source = S3ArchiveRef.model_validate(
        {"uri": f"s3://source/{archive_sha}.tar.gz", "sha256": archive_sha}
    )
    suite_sha = "d" * 64
    digest = "sha256:" + "b" * 64
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
            "test_suite_ref": {"uri": f"s3://tests/{suite_sha}.tar.gz", "sha256": suite_sha},
            "attempts": [],
            "cleanup_status": "CLEANED",
        }
    )
    build = BuildAttempt.model_validate(
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
    )
    target = TargetSelectionV2.model_validate(
        {
            "ref": ref("target"),
            "provider": "ON_PREM",
            "location": None,
            "access_ref": ref("cluster"),
            "capabilities_ref": ref("caps"),
        }
    )
    environment = EnvironmentInputV2.model_validate(
        {
            "env_file_ref": {**ref("env"), "sha256": "e" * 64},
            "mapping_ref": {**ref("mapping"), "sha256": "f" * 64},
        }
    )
    return (
        {
            "report": report,
            "sandbox": sandbox,
            "requirements_ref": requirements_ref,
            "source_refs": {"app": source},
            "builds": [build],
            "target_selection": target,
            "environment": environment,
            "policy": PlanningPolicyV2(template_version="v2"),
        },
        content,
        requirements,
        env,
    )


def test_fixture_projects_to_planning(planning_inputs):
    inputs, content, requirements, env = planning_inputs
    request = project_planning(**inputs)
    assert (
        SandboxVerification.model_validate_json(inputs["sandbox"].model_dump_json())
        == inputs["sandbox"]
    )
    assert request.environment.mapping_ref == inputs["environment"].mapping_ref
    assert env.excluded == ["AWS_REGION"]
    result = prepare_plan_v2(request.model_dump(mode="json"), requirements_bytes=content)
    assert result.spec.images[0].image_digest == inputs["builds"][0].image_digest
    assert result.spec.requirements == requirements
    assert request.analysis.result_ref == inputs["report"].ref


@pytest.mark.parametrize("change", ["analysis", "target", "sandbox_status", "requirements"])
def test_stale_or_unpassed_evidence_is_rejected(planning_inputs, change):
    inputs, content, _, _ = planning_inputs
    if change == "target":
        data = inputs["target_selection"].model_dump(mode="json")
        data["ref"]["id"] = "different-target"
        inputs["target_selection"] = TargetSelectionV2.model_validate(data)
    elif change == "requirements":
        data = inputs["requirements_ref"].model_dump(mode="json")
        data["sha256"] = "0" * 64
        inputs["requirements_ref"] = ContentRef.model_validate(data)
    else:
        data = inputs["sandbox"].model_dump(mode="json")
        if change == "analysis":
            data["analysis_ref"]["id"] = "old-analysis"
        else:
            data["status"] = "NEEDS_REVIEW"
        inputs["sandbox"] = SandboxVerification.model_validate(data)
    with pytest.raises(ValueError):
        request = project_planning(**inputs)
        prepare_plan_v2(request.model_dump(mode="json"), requirements_bytes=content)
