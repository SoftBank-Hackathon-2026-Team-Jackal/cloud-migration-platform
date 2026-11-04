"""Verification contracts and deterministic Planning input preparation."""

import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.provisioning import ContentRef, Contract, VersionRef
from cmp_contracts.provisioning_v2 import (
    AnalysisInputV2,
    BuildEvidenceV2,
    EnvironmentInputV2,
    PlanningPolicyV2,
    PlanningRequestV2,
    RequirementsV2,
    ResourceId,
    S3ArchiveRef,
    SandboxEvidenceV2,
    TargetSelectionV2,
    ValidatedWorkloadV2,
)

SourceArchive = S3ArchiveRef


class BuildLogRef(Contract):
    uri: str
    version_id: str | None = None

    @model_validator(mode="after")
    def validate_uri(self):
        parts = urlsplit(self.uri)
        if parts.scheme != "s3" or not parts.netloc or not parts.path.strip("/"):
            raise ValueError("Expected an S3 log reference")
        if parts.query or parts.fragment or ".." in parts.path.split("/"):
            raise ValueError("Invalid S3 log reference")
        return self


class BuildAttempt(Contract):
    migration_id: str
    resource_id: str
    attempt_id: str
    result_ref: VersionRef
    status: Literal["SUCCEEDED", "FAILED"]
    source_ref: S3ArchiveRef
    platform: Literal["linux/amd64", "linux/arm64"]
    image_uri: str | None = None
    image_digest: str | None = None
    log: BuildLogRef | None = None
    error_code: str | None = None


class Verdict(Contract):
    passed: bool
    reasons: list[str]


class SandboxAttempt(Contract):
    attempt: int = Field(ge=1, le=3)
    verdict: Verdict
    failure_class: Literal["SPEC", "CODE", "BUILD", "RENDER"] | None = None


class SandboxVerification(Contract):
    migration_id: str
    ref: VersionRef
    analysis_ref: VersionRef
    target_selection_ref: VersionRef
    status: Literal["PASSED", "FAILED", "NEEDS_REVIEW", "STOPPED_RENDER"]
    validated_workloads: list[ValidatedWorkloadV2]
    requirements_ref: ContentRef
    report_ref: ContentRef
    test_suite_ref: S3ArchiveRef
    attempts: list[SandboxAttempt]
    cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]
    config_artifact_ref: ContentRef | None = None


def canonical_requirements(requirements: RequirementsV2) -> tuple[bytes, str]:
    """Return stable UTF-8 JSON bytes and their SHA-256; preserve list order."""
    data = json.dumps(
        requirements.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return data, hashlib.sha256(data).hexdigest()


def project_planning(
    *,
    report: AnalysisReport,
    sandbox: SandboxVerification,
    requirements_ref: ContentRef,
    source_refs: dict[ResourceId, S3ArchiveRef],
    builds: list[BuildAttempt],
    target_selection: TargetSelectionV2,
    environment: EnvironmentInputV2,
    policy: PlanningPolicyV2,
    config_artifact_ref: ContentRef | None = None,
) -> PlanningRequestV2:
    """Project recorded evidence; Planning separately validates artifact lineage."""
    if (
        sandbox.status != "PASSED"
        or report.migration_id != sandbox.migration_id
        or sandbox.analysis_ref != report.ref
    ):
        raise ValueError("A passed Sandbox for the same migration is required")
    normalized_builds = []
    for build in builds:
        if build.migration_id != report.migration_id:
            raise ValueError("Build belongs to a different migration")
        normalized_builds.append(
            BuildEvidenceV2(
                resource_id=build.resource_id,
                result_ref=build.result_ref,
                status=build.status,
                source_ref=build.source_ref,
                image_uri=build.image_uri,
                image_digest=build.image_digest,
                platforms=[build.platform],
            )
        )
    request = PlanningRequestV2(
        contract_version="2",
        migration_id=report.migration_id,
        analysis=AnalysisInputV2(
            result_ref=report.ref,
            requirements_ref=requirements_ref,
            source_refs=source_refs,
            config_artifact_ref=config_artifact_ref,
        ),
        builds=normalized_builds,
        sandbox=SandboxEvidenceV2(
            result_ref=sandbox.ref,
            status="PASSED",
            validated_workloads=sandbox.validated_workloads,
            requirements_ref=sandbox.requirements_ref,
            report_ref=sandbox.report_ref,
            test_suite_ref=sandbox.test_suite_ref,
            cleanup_status=sandbox.cleanup_status,
            config_artifact_ref=sandbox.config_artifact_ref,
        ),
        target_selection=target_selection,
        environment=environment,
        policy=policy,
    )
    if request.target_selection.ref != sandbox.target_selection_ref:
        raise ValueError("Sandbox target selection differs from Planning target")
    return request
