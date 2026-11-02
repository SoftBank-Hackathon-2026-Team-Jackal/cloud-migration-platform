"""Pure plan completeness and approval-identity checks. No execution or authorization."""

from cmp_contracts.plan import ApprovalTarget, PlanManifest, Preview, Scope
from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue

from cmp_providers.artifacts import sha256_json


def manifest_digest(manifest: PlanManifest) -> str:
    return sha256_json(manifest.model_dump(mode="json"))


def approval_issues(manifest: PlanManifest, preview: Preview) -> list[ValidationIssue]:
    issues = []

    def reject(code, path, message):
        issues.append(ValidationIssue(code=code, path=path, message=message))

    if preview.manifest_digest != manifest_digest(manifest):
        reject("STALE_PREVIEW", "preview.manifest_digest", "Preview describes a different plan.")
    required = {"spec"}
    if manifest.scope in ("INFRASTRUCTURE", "FULL") and manifest.target.provider != "ON_PREM":
        required.add("terraform_plan")
    if manifest.scope in ("WORKLOADS", "FULL"):
        required.update(
            {
                "database_chart",
                "database_values",
                "application_chart",
                "application_values",
                "bindings",
            }
        )
        if manifest.image_uri is None:
            reject(
                "MISSING_IMAGE", "manifest.image_uri", "Workloads require a digest-pinned image."
            )
    for role in sorted(required - manifest.artifacts.keys()):
        reject(
            "MISSING_ARTIFACT",
            f"manifest.artifacts.{role}",
            "Execution scope lacks a required artifact.",
        )
    if preview.errors:
        reject("VALIDATION_FAILED", "preview.errors", "Resolve validation errors before approval.")
    if preview.pending_decisions:
        reject(
            "PENDING_DECISION", "preview.pending_decisions", "Resolve decisions before approval."
        )
    if not preview.changes:
        reject(
            "EMPTY_PREVIEW",
            "preview.changes",
            "Preview must describe changes or explicit no-change results.",
        )
    return issues


def approval_target(manifest: PlanManifest, preview: Preview) -> ApprovalTarget:
    """Identify a complete plan that may be presented for approval; this grants no permission."""
    issues = approval_issues(manifest, preview)
    if issues:
        raise ProvisioningInputError(issues)
    return ApprovalTarget(
        plan_id=manifest.plan.plan_id,
        revision=manifest.plan.revision,
        scope=manifest.scope,
        manifest_digest=manifest_digest(manifest),
        preview_digest=sha256_json(preview.model_dump(mode="json")),
    )


def verify_approval_target(
    manifest: PlanManifest,
    preview: Preview,
    recorded_target: ApprovalTarget,
    *,
    requested_scope: Scope,
) -> None:
    """Compare with identity loaded from a trusted approval record and the CURRENT plan.

    Caller must check approving principal, decision, active Revision, state drift and actual
    artifact bytes. Never trust a client-supplied ApprovalTarget as an approval record.
    """
    current = approval_target(manifest, preview)
    if current != recorded_target or current.scope != requested_scope:
        raise ProvisioningInputError(
            [
                ValidationIssue(
                    code="APPROVAL_TARGET_MISMATCH",
                    path="approval",
                    message="Approval does not identify the current plan, preview and requested scope.",
                )
            ]
        )
