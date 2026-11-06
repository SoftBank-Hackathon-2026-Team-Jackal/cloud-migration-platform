"""Turn a passed Sandbox run into the Planning input (ticket 15, AV-018). Pure and deterministic.

It reads nothing and writes nothing: the Activities load the stored documents, call
`assemble_registration`, and store or register the result. The projection itself is
`project_planning` (ticket 01); this module only builds its inputs from recorded evidence and
refuses anything that is not backed by evidence (no build, a different target, a bad contract).
"""

import hashlib
import json
from typing import Any, Literal

from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.env_plan import EnvPlan
from cmp_contracts.plan_publish import PlanningInputRegistration, TargetSettings
from cmp_contracts.provisioning import ContentRef, VersionRef
from cmp_contracts.provisioning_v2 import (
    EnvironmentInputV2,
    PlanningPolicyV2,
    S3ArchiveRef,
    TargetSelectionV2,
)
from cmp_contracts.verification import (
    BuildAttempt,
    SandboxVerification,
    Verdict,
    project_planning,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from cmp_worker.provisioning_v2 import ProvisioningInputError, prepare_plan_v2


class HandoffError(Exception):
    """`code` is stable and safe to show; nothing from the evidence is put in the message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PlanningSettings(BaseModel):
    """Start-up choices for the hand-off. References and names only; no credential fits here.

    `env_file_ref` is supplied here until the platform stores the `.env` and passes its reference
    in the start request (ENV-02).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_selection: TargetSelectionV2
    target: TargetSettings
    # Left out, the start request's reference is used (the parent copies it in, ENV-02).
    env_file_ref: ContentRef | None = None
    template_version: str = "v2"


def assemble_registration(
    *,
    report: AnalysisReport,
    requirements_ref: ContentRef,
    report_ref: ContentRef,
    builds: list[BuildAttempt],
    attempts: list[dict[str, Any]],
    suite_ref: S3ArchiveRef,
    mapping_ref: ContentRef,
    settings: PlanningSettings,
    expected_target_ref: VersionRef | None = None,
    env_plan: EnvPlan | None = None,
    cleanup_status: Literal["CLEANED", "FAILED", "PENDING"] = "CLEANED",
) -> PlanningInputRegistration:
    """`expected_target_ref` is the target the Sandbox verified, when it recorded one; it must be
    the Planning target. `cleanup_status` is the Sandbox's teardown state (ticket 55); anything
    but CLEANED becomes a `CLEANUP_POLICY_REQUIRED` decision on the Plan."""
    if not builds or any(b.status != "SUCCEEDED" or not b.image_digest for b in builds):
        raise HandoffError("BUILD_EVIDENCE_MISSING")
    if settings.env_file_ref is None:
        raise HandoffError("ENV_FILE_REF_MISSING")
    if env_plan is not None and env_plan.env_file_ref != settings.env_file_ref:
        raise HandoffError("ENV_PLAN_REF_MISMATCH")
    if expected_target_ref is not None and expected_target_ref != settings.target_selection.ref:
        raise HandoffError("TARGET_SELECTION_CHANGED")
    workloads = [
        {
            "resource_id": b.resource_id,
            "build_ref": b.result_ref.model_dump(mode="json"),
            "image_digest": b.image_digest,
            "platform": b.platform,
            "source_sha256": b.source_ref.sha256,
        }
        for b in builds
    ]
    recorded = [
        {
            "attempt": a["attempt"],
            "verdict": Verdict(passed=a["passed"], reasons=a.get("reasons") or []).model_dump(),
            "failure_class": a.get("failure_class"),
        }
        for a in attempts
    ]
    digest = hashlib.sha256(
        json.dumps(
            [workloads, recorded, suite_ref.sha256, requirements_ref.sha256],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    try:
        sandbox = SandboxVerification.model_validate(
            {
                "migration_id": report.migration_id,
                "ref": {"id": f"sandbox-{report.migration_id}", "version": digest[:16]},
                "analysis_ref": report.ref.model_dump(mode="json"),
                "target_selection_ref": settings.target_selection.ref.model_dump(mode="json"),
                "status": "PASSED",
                "validated_workloads": workloads,
                "requirements_ref": requirements_ref.model_dump(mode="json"),
                "report_ref": report_ref.model_dump(mode="json"),
                "test_suite_ref": suite_ref.model_dump(mode="json"),
                "attempts": recorded,
                "cleanup_status": cleanup_status,
            }
        )
        request = project_planning(
            report=report,
            sandbox=sandbox,
            requirements_ref=requirements_ref,
            source_refs={b.resource_id: b.source_ref for b in builds},
            builds=builds,
            target_selection=settings.target_selection,
            environment=EnvironmentInputV2(
                env_file_ref=settings.env_file_ref, mapping_ref=mapping_ref
            ),
            policy=PlanningPolicyV2(template_version=settings.template_version),
        )
        return PlanningInputRegistration(
            contract_version="1",
            planning_request=request,
            target=settings.target,
            env_plan=env_plan,
        )
    except (ValueError, ValidationError):
        raise HandoffError("PROJECTION_REJECTED") from None


def check_with_planning(registration: PlanningInputRegistration, requirements_bytes: bytes) -> None:
    """The same validation Planning runs, so a refusal shows up here with a code."""
    try:
        prepare_plan_v2(
            registration.planning_request.model_dump(mode="json"),
            requirements_bytes=requirements_bytes,
        )
    except ProvisioningInputError:
        raise HandoffError("PLANNING_INPUT_INVALID") from None
