from pathlib import Path

import pytest
from cmp_contracts.plan import PlanManifest, Preview
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.plans import approval_target, manifest_digest, verify_approval_target

FIXTURES = Path(__file__).parent / "fixtures/contracts"


def plan(provider="aws"):
    return PlanManifest.model_validate_json((FIXTURES / f"{provider}-plan.json").read_text())


def preview(manifest):
    return Preview(
        contract_version="1",
        manifest_digest=manifest_digest(manifest),
        changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "demo"}],
    )


@pytest.mark.parametrize("provider", ["aws", "gcp", "on_prem"])
def test_complete_plan_can_identify_exact_approval_target(provider):
    manifest = plan(provider)
    display = preview(manifest)
    approval = approval_target(manifest, display)
    assert approval.plan_id == "plan-001"
    assert approval.revision == 1
    assert approval.scope == "FULL"
    assert PlanManifest.model_validate_json(manifest.model_dump_json()) == manifest
    verify_approval_target(manifest, display, approval, requested_scope="FULL")


def test_missing_terraform_plan_cannot_be_approved():
    payload = plan().model_dump()
    del payload["artifacts"]["terraform_plan"]
    manifest = PlanManifest.model_validate(payload)
    with pytest.raises(ProvisioningInputError) as caught:
        approval_target(manifest, preview(manifest))
    assert "MISSING_ARTIFACT" in {issue.code for issue in caught.value.issues}


@pytest.mark.parametrize(
    "mutation", ["revision", "account", "artifact", "image", "template", "state"]
)
def test_approval_identity_cannot_be_reused_after_plan_changes(mutation):
    original = plan()
    approval = approval_target(original, preview(original))
    payload = original.model_dump()
    if mutation == "revision":
        payload["plan"]["revision"] = 2
    elif mutation == "account":
        payload["target"]["account_id"] = "another-account"
    elif mutation == "artifact":
        payload["artifacts"]["database_values"]["sha256"] = "9" * 64
    elif mutation == "image":
        payload["image_uri"] = "registry/app@sha256:" + "9" * 64
    elif mutation == "template":
        payload["plan"]["template_version"] = "v2"
    else:
        payload["target"]["state_ref"]["id"] = "other-state"
    current = PlanManifest.model_validate(payload)
    with pytest.raises(ProvisioningInputError, match="APPROVAL_TARGET_MISMATCH"):
        verify_approval_target(current, preview(current), approval, requested_scope="FULL")


def test_stale_preview_cannot_be_attached_to_a_new_manifest():
    original = plan()
    display = preview(original)
    payload = original.model_dump()
    payload["plan"]["revision"] = 2
    with pytest.raises(ProvisioningInputError, match="STALE_PREVIEW"):
        approval_target(PlanManifest.model_validate(payload), display)


def test_changed_preview_requires_new_approval():
    manifest = plan()
    display = preview(manifest)
    approval = approval_target(manifest, display)
    display.changes[0].name = "changed-display"
    with pytest.raises(ProvisioningInputError, match="APPROVAL_TARGET_MISMATCH"):
        verify_approval_target(manifest, display, approval, requested_scope="FULL")


def test_infrastructure_approval_never_authorizes_workloads():
    payload = plan().model_dump()
    payload["scope"] = "INFRASTRUCTURE"
    payload["artifacts"] = {
        key: value
        for key, value in payload["artifacts"].items()
        if key in ("spec", "terraform_plan")
    }
    payload["image_uri"] = None
    manifest = PlanManifest.model_validate(payload)
    display = preview(manifest)
    approval = approval_target(manifest, display)
    verify_approval_target(manifest, display, approval, requested_scope="INFRASTRUCTURE")
    for scope in ("WORKLOADS", "FULL"):
        with pytest.raises(ProvisioningInputError, match="APPROVAL_TARGET_MISMATCH"):
            verify_approval_target(manifest, display, approval, requested_scope=scope)


@pytest.mark.parametrize(
    "field,code", [("errors", "VALIDATION_FAILED"), ("pending_decisions", "PENDING_DECISION")]
)
def test_unresolved_preview_cannot_be_approved(field, code):
    manifest = plan()
    payload = preview(manifest).model_dump()
    payload[field] = [{"code": "TEST", "path": "input", "message": "Needs resolution"}]
    with pytest.raises(ProvisioningInputError, match=code):
        approval_target(manifest, Preview.model_validate(payload))


@pytest.mark.parametrize(
    "role",
    [
        "spec",
        "database_chart",
        "database_values",
        "application_chart",
        "application_values",
        "bindings",
    ],
)
def test_full_scope_requires_each_execution_artifact(role):
    payload = plan().model_dump()
    del payload["artifacts"][role]
    manifest = PlanManifest.model_validate(payload)
    with pytest.raises(ProvisioningInputError) as caught:
        approval_target(manifest, preview(manifest))
    assert any(issue.path == f"manifest.artifacts.{role}" for issue in caught.value.issues)


def test_warnings_are_visible_and_part_of_approval_but_not_automatic_errors():
    manifest = plan()
    payload = preview(manifest).model_dump()
    payload["warnings"] = [
        {"code": "LIMIT", "path": "network", "message": "Review outbound policy"}
    ]
    assert approval_target(manifest, Preview.model_validate(payload))


@pytest.mark.parametrize(
    "change", ["version", "role", "provider", "onprem_infra", "onprem_terraform"]
)
def test_malformed_plan_contract_is_rejected(change):
    from pydantic import ValidationError

    payload = plan("on_prem" if change.startswith("onprem") else "aws").model_dump()
    if change == "version":
        payload["contract_version"] = "2"
    elif change == "role":
        payload["artifacts"]["spec"]["kind"] = "CHART"
    elif change == "provider":
        payload["target"]["provider"] = "AZURE"
    elif change == "onprem_infra":
        payload["scope"] = "INFRASTRUCTURE"
    else:
        payload["artifacts"]["terraform_plan"] = plan().model_dump()["artifacts"]["terraform_plan"]
    with pytest.raises(ValidationError):
        PlanManifest.model_validate(payload)


def test_missing_image_and_empty_preview_cannot_be_approved():
    payload = plan().model_dump()
    payload["image_uri"] = None
    manifest = PlanManifest.model_validate(payload)
    display = Preview(contract_version="1", manifest_digest=manifest_digest(manifest), changes=[])
    with pytest.raises(ProvisioningInputError) as caught:
        approval_target(manifest, display)
    assert {issue.code for issue in caught.value.issues} == {"MISSING_IMAGE", "EMPTY_PREVIEW"}
