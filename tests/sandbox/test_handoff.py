"""Ticket 15: the Planning hand-off assembly (fixed evidence, Planning's own validation)."""

import json
from pathlib import Path

import pytest
from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.provisioning import ContentRef, VersionRef
from cmp_contracts.provisioning_v2 import RequirementsV2, S3ArchiveRef, TargetSelectionV2
from cmp_contracts.verification import BuildAttempt, canonical_requirements
from cmp_worker.planning_handoff import (
    HandoffError,
    PlanningSettings,
    assemble_registration,
    check_with_planning,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analysis"
DIGEST = "sha256:" + "b" * 64


def ref(name, version="1"):
    return {"id": name, "version": version}


def evidence(**kw):
    report = AnalysisReport.model_validate(
        json.loads((FIXTURES / "analysis-report-eks-k3s.json").read_text(encoding="utf-8"))[
            "report"
        ]
    )
    requirements = RequirementsV2.model_validate_json(
        (FIXTURES / "requirements-eks-k3s.json").read_bytes()
    )
    content, sha = canonical_requirements(requirements)
    src = "a" * 64
    source = S3ArchiveRef(uri=f"s3://cmp-sources/sources/{src}.tar.gz", sha256=src)
    build = BuildAttempt(
        migration_id=report.migration_id, resource_id="app", attempt_id="run1-b1",
        result_ref=VersionRef(**ref("build-app")), status="SUCCEEDED", source_ref=source,
        platform="linux/amd64", image_uri=f"registry.example/app@{DIGEST}", image_digest=DIGEST,
    )  # fmt: skip
    suite = "d" * 64
    settings = PlanningSettings(
        target_selection=TargetSelectionV2.model_validate(
            {
                "ref": ref("target"),
                "provider": "ON_PREM",
                "location": None,
                "access_ref": ref("cluster"),
                "capabilities_ref": ref("caps"),
            }
        ),
        target={
            "provider": "ON_PREM",
            "environment_id": "env-1",
            "agent_id": "agent-1",
            "cluster_uid": "uid-1",
            "namespace": "demo",
            "storage_class": "local-path",
            "database_image": "docker.io/library/postgres@sha256:" + "c" * 64,
        },
        env_file_ref=ContentRef(**ref("env"), sha256="e" * 64),
    )
    args = {
        "report": report,
        "requirements_ref": ContentRef(**ref("requirements"), sha256=sha),
        "report_ref": ContentRef(**ref("report"), sha256="c" * 64),
        "builds": [build],
        "attempts": [{"attempt": 1, "passed": True}],
        "suite_ref": S3ArchiveRef(uri=f"s3://cmp-sources/tests/{suite}.tar.gz", sha256=suite),
        "mapping_ref": ContentRef(**ref("mapping"), sha256="f" * 64),
        "settings": settings,
    }
    args.update(kw)
    return args, content


def test_the_assembly_passes_the_planning_validation_and_digests_match():
    args, content = evidence()
    registration = assemble_registration(**args)
    check_with_planning(registration, content)
    request = registration.planning_request
    assert request.builds[0].image_digest == request.sandbox.validated_workloads[0].image_digest
    assert request.sandbox.test_suite_ref == args["suite_ref"]
    assert request.environment.mapping_ref == args["mapping_ref"]
    assert registration.target.provider == "ON_PREM"


def test_no_build_evidence_means_no_hand_off():
    args, _ = evidence(builds=[])
    with pytest.raises(HandoffError) as exc:
        assemble_registration(**args)
    assert exc.value.code == "BUILD_EVIDENCE_MISSING"


def test_a_failed_build_is_not_evidence():
    args, _ = evidence()
    failed = args["builds"][0].model_copy(update={"status": "FAILED", "image_digest": None})
    with pytest.raises(HandoffError) as exc:
        assemble_registration(**{**args, "builds": [failed]})
    assert exc.value.code == "BUILD_EVIDENCE_MISSING"


def test_a_target_that_changed_after_verification_is_not_handed_over():
    args, _ = evidence()
    with pytest.raises(HandoffError) as exc:
        assemble_registration(**args, expected_target_ref=VersionRef(**ref("target", "0")))
    assert exc.value.code == "TARGET_SELECTION_CHANGED"
    assemble_registration(**args, expected_target_ref=VersionRef(**ref("target")))  # same: fine


def test_a_report_from_another_analysis_is_refused():
    args, _ = evidence()
    other = args["report"].model_copy(update={"migration_id": "other"})
    with pytest.raises(HandoffError) as exc:
        assemble_registration(
            **{
                **args,
                "report": other,
                "builds": [args["builds"][0].model_copy(update={"migration_id": "migration-1"})],
            }
        )
    assert exc.value.code in {"PROJECTION_REJECTED", "BUILD_EVIDENCE_MISSING"}


def test_a_digest_that_differs_from_the_requirements_is_caught_by_the_planning_check():
    args, content = evidence()
    registration = assemble_registration(**args)
    with pytest.raises(HandoffError) as exc:
        check_with_planning(registration, content + b" ")
    assert exc.value.code == "PLANNING_INPUT_INVALID"


def test_the_same_evidence_always_gives_the_same_registration():
    a, _ = evidence()
    b, _ = evidence()
    assert (
        assemble_registration(**a).model_dump_json() == assemble_registration(**b).model_dump_json()
    )


def test_the_start_input_carries_planning_settings_only_when_given_and_rejects_typos():
    from cmp_worker.start_input import build_start_input

    settings = evidence()[0]["settings"].model_dump(mode="json")
    plain = build_start_input(project_id="p", repository="https://github.com/a/b", commit="a" * 40)
    assert "planning" not in plain
    given = build_start_input(
        project_id="p", repository="https://github.com/a/b", commit="a" * 40,
        planning_json=json.dumps(settings),
    )  # fmt: skip
    assert given["planning"]["target_selection"]["ref"] == {"id": "target", "version": "1"}
    with pytest.raises(ValueError):
        build_start_input(
            project_id="p", repository="https://github.com/a/b", commit="a" * 40,
            planning_json=json.dumps({**settings, "env_file": "typo"}),
        )  # fmt: skip
    assert "secret" not in json.dumps(given["planning"]).lower()
