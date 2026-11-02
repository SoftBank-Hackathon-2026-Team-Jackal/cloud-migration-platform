"""Pure Planning entry point for resolved metadata. I/O belongs in future Activities."""

from cmp_contracts.provisioning import (
    InputLineage,
    PlanningRequest,
    PlanningResult,
    ProvisioningInputError,
    ProvisioningSpec,
    ValidationIssue,
)
from cmp_providers.provisioning import adapt
from pydantic import ValidationError


def prepare_plan(payload: dict) -> PlanningResult:
    """Validate a resolved internal request and produce a spec/configuration, not a Preview."""
    try:
        request = PlanningRequest.model_validate(payload)
    except ValidationError as error:
        raise ProvisioningInputError(
            [
                ValidationIssue(
                    code="INVALID_INPUT",
                    path=".".join(str(part) for part in item["loc"]),
                    message="Missing, unsupported or malformed input; check the internal v1 contract.",
                )
                for item in error.errors(include_input=False, include_context=False)
            ]
        ) from None
    issues = []
    analysis, sandbox, build = request.analysis, request.sandbox, request.build
    for stage_name in ("analysis", "sandbox", "build"):
        if getattr(request, stage_name).migration_id != request.migration_id:
            issues.append(
                ValidationIssue(
                    code="LINEAGE_MISMATCH",
                    path=f"{stage_name}.migration_id",
                    message="Stage result belongs to a different migration.",
                )
            )
    comparisons = [
        ("sandbox.analysis_ref", sandbox.analysis_ref, analysis.ref),
        ("sandbox.source", sandbox.source, analysis.source),
        ("sandbox.config", sandbox.config, analysis.config),
        ("sandbox.requirements_ref", sandbox.requirements_ref, analysis.requirements_ref),
        ("sandbox.environment_ref", sandbox.environment_ref, request.environment_ref),
        (
            "sandbox.target_selection_ref",
            sandbox.target_selection_ref,
            request.target_selection.ref,
        ),
        ("build.sandbox_ref", build.sandbox_ref, sandbox.ref),
        ("build.source", build.source, sandbox.source),
        ("build.config", build.config, sandbox.config),
    ]
    for path, actual, expected in comparisons:
        if actual != expected:
            issues.append(
                ValidationIssue(
                    code="LINEAGE_MISMATCH",
                    path=path,
                    message="Input version differs from its validated predecessor; revalidate/rebuild.",
                )
            )
    for path, actual, expected in [
        ("sandbox.status", sandbox.status, "PASSED"),
        ("build.status", build.status, "SUCCEEDED"),
    ]:
        if actual != expected:
            issues.append(
                ValidationIssue(
                    code="PRECONDITION_FAILED",
                    path=path,
                    message="Required validation or build stage has not succeeded.",
                )
            )
    if issues:
        raise ProvisioningInputError(issues)
    spec = ProvisioningSpec(
        contract_version=request.contract_version,
        migration_id=request.migration_id,
        plan=request.plan,
        inputs=InputLineage(
            analysis_ref=request.analysis.ref,
            sandbox_ref=request.sandbox.ref,
            build_ref=request.build.ref,
            requirements_ref=request.analysis.requirements_ref,
            source=request.analysis.source,
            config=request.analysis.config,
            target_selection_ref=request.target_selection.ref,
            sandbox_report_ref=request.sandbox.report_ref,
            sandbox_cleanup_status=request.sandbox.cleanup_status,
        ),
        target=request.target_selection.target,
        image=request.build.image,
        requirements=request.analysis.requirements,
        environment_ref=request.environment_ref,
    )
    pending = []
    if sandbox.cleanup_status != "CLEANED":
        pending.append(
            ValidationIssue(
                code="CLEANUP_POLICY_REQUIRED",
                path="sandbox.cleanup_status",
                message="Cleanup remains unresolved; decide progression policy before approving execution.",
            )
        )
    return PlanningResult(spec=spec, configuration=adapt(spec), pending_decisions=pending)
