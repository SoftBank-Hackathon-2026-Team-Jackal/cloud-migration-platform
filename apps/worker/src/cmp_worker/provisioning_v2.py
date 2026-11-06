"""Pure v2 Planning validation after an Activity resolves authorized artifact bytes."""

import json

from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue
from cmp_contracts.provisioning_v2 import (
    ApplicationRequirement,
    PlanningRequestV2,
    PlanningResultV2,
    PostgresRequirement,
    ProvisioningSpecV2,
    RequirementsV2,
    ValidatedImageV2,
)
from cmp_providers.artifacts import sha256_bytes
from pydantic import ValidationError

_MAX_REQUIREMENTS_BYTES = 1024 * 1024
_FIELD_NAMES = frozenset(
    name
    for model in (
        PlanningRequestV2,
        RequirementsV2,
        ApplicationRequirement,
        PostgresRequirement,
    )
    for name in model.model_fields
) | frozenset(
    {
        "uri",
        "version_id",
        "sha256",
        "result_ref",
        "requirements_ref",
        "source_refs",
        "config_artifact_ref",
        "resource_id",
        "output",
        "source_resource_id",
        "destination_resource_id",
        "external_host",
        "protocol",
        "name",
        "username",
        "image_uri",
        "image_digest",
        "platforms",
        "build_ref",
        "platform",
        "source_sha256",
        "validated_workloads",
        "report_ref",
        "test_suite_ref",
        "cleanup_status",
        "ref",
        "provider",
        "location",
        "access_ref",
        "capabilities_ref",
        "profile",
        "env_file_ref",
        "mapping_ref",
        "template_version",
        "status",
        "version",
        "kind",
        "database",
        "extensions",
        "port",
        "health_path",
        "replicas",
        "cpu_millicores",
        "memory_mib",
        "storage_gib",
    }
)


def _issue(code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=message)


def _schema_issues(error: ValidationError, code: str, prefix: str = "") -> list[ValidationIssue]:
    # Pydantic locations may contain arbitrary mapping keys supplied by callers.
    # Never reflect those keys into an error that may enter logs or Temporal history.
    return [
        _issue(
            code,
            ".".join(
                [
                    part
                    for part in (
                        prefix,
                        *(
                            str(loc) if isinstance(loc, int) or loc in _FIELD_NAMES else "<key>"
                            for loc in item["loc"]
                        ),
                    )
                    if part
                ]
            ),
            "Missing, unsupported or malformed field.",
        )
        for item in error.errors(include_input=False, include_context=False)
    ]


def _requirements(content: bytes, expected_sha: str) -> RequirementsV2:
    if type(content) is not bytes or len(content) > _MAX_REQUIREMENTS_BYTES:
        raise ProvisioningInputError(
            [_issue("INVALID_ARTIFACT", "analysis.requirements_ref", "Invalid artifact bytes.")]
        )
    if sha256_bytes(content) != expected_sha:
        raise ProvisioningInputError(
            [
                _issue(
                    "ARTIFACT_DIGEST_MISMATCH",
                    "analysis.requirements_ref.sha256",
                    "Resolved requirements bytes differ from their reference.",
                )
            ]
        )
    try:
        data = json.loads(content)
        return RequirementsV2.model_validate(data)
    except (UnicodeError, ValueError, TypeError) as error:
        if isinstance(error, ValidationError):
            issues = _schema_issues(error, "INVALID_REQUIREMENTS", "requirements")
        else:
            issues = [
                _issue("INVALID_REQUIREMENTS", "requirements", "Requirements are not valid JSON.")
            ]
        raise ProvisioningInputError(issues) from None


def prepare_plan_v2(payload: dict, *, requirements_bytes: bytes) -> PlanningResultV2:
    """Validate v2 lineage and return Plan inputs; never create a Plan or cloud resource.

    The caller resolves and authorizes artifact refs, supplies exact requirements
    bytes, and separately verifies environment/report/test/capability artifacts.
    """
    try:
        request = PlanningRequestV2.model_validate(payload)
    except ValidationError as error:
        raise ProvisioningInputError(_schema_issues(error, "INVALID_INPUT")) from None
    requirements = _requirements(requirements_bytes, request.analysis.requirements_ref.sha256)
    issues: list[ValidationIssue] = []

    def reject(code: str, path: str, message: str) -> None:
        issues.append(_issue(code, path, message))

    resources = {}
    applications = set()
    database_count = 0
    for index, resource in enumerate(requirements.resources):
        if resource.id in resources:
            reject(
                "DUPLICATE_RESOURCE_ID",
                f"requirements.resources.{index}.id",
                "Resource ID repeats.",
            )
        resources[resource.id] = resource
        if isinstance(resource, ApplicationRequirement):
            applications.add(resource.id)
        if isinstance(resource, PostgresRequirement):
            database_count += 1
            if resource.extensions:
                reject(
                    "UNSUPPORTED_DB_EXTENSION",
                    f"requirements.resources.{index}.extensions",
                    "P0 cannot verify PostgreSQL extensions.",
                )
    if not applications:
        reject("MISSING_RESOURCE_KIND", "requirements.resources", "An application is required.")
    if not database_count:
        reject("MISSING_RESOURCE_KIND", "requirements.resources", "PostgreSQL is required.")

    for index, resource in enumerate(requirements.resources):
        if isinstance(resource, ApplicationRequirement):
            for binding in resource.env_bindings.values():
                target = resources.get(binding.resource_id)
                path = f"requirements.resources.{index}.env_bindings.<env>.resource_id"
                if target is None:
                    reject("UNKNOWN_RESOURCE_REF", path, "Binding references an unknown resource.")
                elif not isinstance(target, PostgresRequirement):
                    reject(
                        "UNSUPPORTED_RESOURCE_OUTPUT",
                        path,
                        "Only PostgreSQL outputs are supported.",
                    )
    for index, intent in enumerate(requirements.network_intent):
        if intent.source_resource_id not in resources:
            reject(
                "UNKNOWN_RESOURCE_REF",
                f"requirements.network_intent.{index}.source_resource_id",
                "Network source is unknown.",
            )
        if intent.destination_resource_id and intent.destination_resource_id not in resources:
            reject(
                "UNKNOWN_RESOURCE_REF",
                f"requirements.network_intent.{index}.destination_resource_id",
                "Network destination is unknown.",
            )

    sources = request.analysis.source_refs
    for _resource_id in sorted(applications - sources.keys()):
        reject(
            "MISSING_SOURCE",
            "analysis.source_refs.<resource>",
            "Application source is missing.",
        )
    for _resource_id in sorted(sources.keys() - applications):
        reject("UNEXPECTED_SOURCE", "analysis.source_refs.<resource>", "Source has no application.")

    if request.sandbox.status != "PASSED":
        reject("PRECONDITION_FAILED", "sandbox.status", "Sandbox did not pass.")
    if request.sandbox.requirements_ref != request.analysis.requirements_ref:
        reject(
            "REQUIREMENTS_MISMATCH",
            "sandbox.requirements_ref",
            "Sandbox validated a different requirements version.",
        )
    if request.sandbox.config_artifact_ref != request.analysis.config_artifact_ref:
        reject(
            "CONFIG_MISMATCH",
            "sandbox.config_artifact_ref",
            "Sandbox configuration evidence differs from Analysis.",
        )

    builds = {}
    for index, build in enumerate(request.builds):
        if build.resource_id in builds:
            reject("DUPLICATE_BUILD", f"builds.{index}.resource_id", "Build resource ID repeats.")
        builds[build.resource_id] = build
        if build.resource_id not in applications:
            reject("UNEXPECTED_BUILD", f"builds.{index}.resource_id", "Build has no application.")
        if build.status != "SUCCEEDED":
            reject("PRECONDITION_FAILED", f"builds.{index}.status", "Build did not succeed.")
        elif build.image_uri is None or build.image_digest is None or not build.platforms:
            reject("MISSING_IMAGE", f"builds.{index}", "Successful build lacks image evidence.")
        elif build.image_uri.rsplit("@", 1)[1] != build.image_digest:
            reject(
                "IMAGE_DIGEST_MISMATCH",
                f"builds.{index}.image_digest",
                "Image URI and Digest differ.",
            )
        expected_source = sources.get(build.resource_id)
        if expected_source is not None and build.source_ref != expected_source:
            reject(
                "SOURCE_MISMATCH",
                f"builds.{index}.source_ref",
                "Build source differs from Analysis.",
            )
    for _resource_id in sorted(applications - builds.keys()):
        reject("MISSING_BUILD", "builds.<resource>", "Application build is missing.")

    validated = {}
    for index, workload in enumerate(request.sandbox.validated_workloads):
        if workload.resource_id in validated:
            reject(
                "DUPLICATE_SANDBOX_VALIDATION",
                f"sandbox.validated_workloads.{index}.resource_id",
                "Sandbox resource ID repeats.",
            )
        validated[workload.resource_id] = workload
        if workload.resource_id not in applications:
            reject(
                "UNEXPECTED_SANDBOX_VALIDATION",
                f"sandbox.validated_workloads.{index}.resource_id",
                "Sandbox validation has no application.",
            )
        build = builds.get(workload.resource_id)
        if build is None:
            continue
        base = f"sandbox.validated_workloads.{index}"
        if workload.build_ref != build.result_ref:
            reject("BUILD_REF_MISMATCH", f"{base}.build_ref", "Sandbox used a different build.")
        if workload.image_digest != build.image_digest:
            reject(
                "IMAGE_DIGEST_MISMATCH", f"{base}.image_digest", "Sandbox ran a different image."
            )
        if workload.source_sha256 != build.source_ref.sha256:
            reject("SOURCE_MISMATCH", f"{base}.source_sha256", "Sandbox source differs from Build.")
        if workload.platform not in build.platforms:
            reject("PLATFORM_MISMATCH", f"{base}.platform", "Sandbox platform lacks Build support.")
    for _resource_id in sorted(applications - validated.keys()):
        reject(
            "MISSING_SANDBOX_VALIDATION",
            "sandbox.validated_workloads.<resource>",
            "Application was not validated in Sandbox.",
        )
    if issues:
        raise ProvisioningInputError(issues)

    images = [
        ValidatedImageV2(
            resource_id=resource_id,
            build_ref=builds[resource_id].result_ref,
            source_ref=builds[resource_id].source_ref,
            image_uri=builds[resource_id].image_uri,
            image_digest=builds[resource_id].image_digest,
            platform=validated[resource_id].platform,
            platforms=list(builds[resource_id].platforms),  # ticket 58
        )
        for resource_id in sorted(applications)
    ]
    spec = ProvisioningSpecV2(
        contract_version="2",
        migration_id=request.migration_id,
        analysis_result_ref=request.analysis.result_ref,
        requirements_ref=request.analysis.requirements_ref,
        requirements=requirements,
        source_refs=sources,
        config_artifact_ref=request.analysis.config_artifact_ref,
        sandbox_result_ref=request.sandbox.result_ref,
        sandbox_report_ref=request.sandbox.report_ref,
        sandbox_cleanup_status=request.sandbox.cleanup_status,
        test_suite_ref=request.sandbox.test_suite_ref,
        images=images,
        target_selection=request.target_selection,
        environment=request.environment,
        policy=request.policy,
    )
    pending = []
    if request.sandbox.cleanup_status != "CLEANED":
        pending.append(
            _issue(
                "CLEANUP_POLICY_REQUIRED",
                "sandbox.cleanup_status",
                "Sandbox cleanup remains unresolved before approval.",
            )
        )
    return PlanningResultV2(spec=spec, pending_decisions=pending)
