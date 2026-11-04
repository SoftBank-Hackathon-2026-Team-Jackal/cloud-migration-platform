"""Ticket 12A seam: 04B-rendered Values bytes + ON_PREM manifest -> Preview -> approval contract."""

import json

import pytest
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_contracts.plan import PlanManifest
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes
from cmp_providers.onprem_preview import onprem_preview
from cmp_providers.plans import approval_issues, approval_target
from onprem_rollout_support import NAMESPACE, build_artifacts, plan

VALUE_ROLES = ("database_values", "application_values")


@pytest.fixture
def built(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    refs = build_artifacts(store, tmp_path)
    manifest = plan(refs)
    contents = {role: store.read(manifest.artifacts[role]) for role in VALUE_ROLES}
    return manifest, contents, store, tmp_path


def by_name(preview):
    return {change.name: change for change in preview.changes}


def test_preview_describes_onprem_rollout_and_is_approvable(built):
    manifest, contents, _, _ = built
    preview = onprem_preview(manifest, contents)

    changes = by_name(preview)
    assert changes["demo-db"].resource_type == "DATABASE"
    assert changes["demo-db"].action == "CREATE"
    assert "PostgreSQL 16" in changes["demo-db"].size
    assert "250m" in changes["demo-db"].size
    pvc = changes["demo-db PVC"]
    assert (pvc.resource_type, pvc.action) == ("STORAGE", "CREATE")
    assert "1 GiB" in pvc.size and "local-path" in pvc.size
    app = changes["demo-app"]
    assert (app.resource_type, app.action, app.quantity) == ("APP", "CREATE", 1)
    assert ":8080" in app.size and "/health" in app.size
    assert "환경변수 6개" in app.size
    assert changes["demo-app Service"].resource_type == "APP"
    ingress = next(c for c in preview.changes if c.resource_type == "INGRESS")
    assert ingress.action == "NO_CHANGE"
    cluster = next(c for c in preview.changes if c.resource_type == "CLUSTER")
    assert cluster.action == "NO_CHANGE"
    assert "cluster-a" in cluster.name and "agent-a" in cluster.name and NAMESPACE in cluster.name

    defaults = {change.name for change in preview.applied_defaults}
    assert any("Namespace" in name for name in defaults)
    assert any("비밀번호 Secret" in name for name in defaults)
    warnings = {issue.code: issue.message for issue in preview.warnings}
    assert "CLUSTER_STATE_NOT_INSPECTED" in warnings
    assert "demo-app-env/API_KEY" in warnings["USER_SECRET_REQUIRED"]

    assert preview.errors == []
    assert approval_issues(manifest, preview) == []
    assert approval_target(manifest, preview).scope == "WORKLOADS"


def test_preview_never_contains_env_or_secret_values(built):
    manifest, contents, _, _ = built
    values = json.loads(contents["application_values"])
    literals = [
        item["source"]["value"] for item in values["env"] if item["source"]["kind"] == "LITERAL"
    ]
    assert literals  # the fixture really has literal values to leak
    dumped = onprem_preview(manifest, contents).model_dump_json()
    for literal in literals:
        assert f'"{literal}"' not in dumped


def test_namespace_mismatch_is_shown_as_error_and_blocks_approval(built):
    manifest, contents, _, _ = built
    other = plan(manifest.model_dump(mode="json")["artifacts"], namespace="another-namespace")
    preview = onprem_preview(other, contents)
    assert [issue.code for issue in preview.errors] == ["VALUES_TARGET_MISMATCH"]
    assert "VALIDATION_FAILED" in {issue.code for issue in approval_issues(other, preview)}


def test_image_mismatch_is_shown_as_error(built):
    manifest, contents, _, _ = built
    data = manifest.model_dump(mode="json")
    data["image_uri"] = "registry.example.com/demo@sha256:" + "9" * 64
    preview = onprem_preview(PlanManifest.model_validate(data), contents)
    assert "IMAGE_MISMATCH" in {issue.code for issue in preview.errors}


def test_values_bytes_must_match_manifest_digest(built):
    manifest, contents, _, _ = built
    changed = dict(contents)
    changed["database_values"] = contents["database_values"].replace(b"appdb", b"other")
    with pytest.raises(ProvisioningInputError) as caught:
        onprem_preview(manifest, changed)
    assert {issue.code for issue in caught.value.issues} == {"ARTIFACT_DIGEST_MISMATCH"}


def test_missing_values_artifact_is_rejected(built):
    manifest, contents, _, _ = built
    with pytest.raises(ProvisioningInputError) as caught:
        onprem_preview(manifest, {"database_values": contents["database_values"]})
    assert {issue.code for issue in caught.value.issues} == {"MISSING_ARTIFACT"}


def test_cloud_manifest_is_rejected(built):
    manifest, contents, _, _ = built
    data = manifest.model_dump(mode="json")
    data["target"] = {
        "provider": "AWS",
        "environment_id": "aws-prod",
        "account_id": "714066696240",
        "region": "ap-northeast-2",
        "state_ref": {"id": "state", "version": "1"},
    }
    with pytest.raises(ProvisioningInputError) as caught:
        onprem_preview(PlanManifest.model_validate(data), contents)
    assert {issue.code for issue in caught.value.issues} == {"UNSUPPORTED_TARGET"}


def test_unparseable_values_are_rejected_without_echoing_content(built):
    manifest, _, store, _ = built
    data = manifest.model_dump(mode="json")
    junk = canonical_json_bytes({"password": "DO_NOT_ECHO"})
    data["artifacts"]["database_values"] = {
        **data["artifacts"]["database_values"],
        "sha256": sha256_bytes(junk),
        "size_bytes": len(junk),
    }
    changed = PlanManifest.model_validate(data)
    contents = {
        "database_values": junk,
        "application_values": store.read(changed.artifacts["application_values"]),
    }
    with pytest.raises(ProvisioningInputError) as caught:
        onprem_preview(changed, contents)
    assert {issue.code for issue in caught.value.issues} == {"INVALID_VALUES_ARTIFACT"}
    assert "DO_NOT_ECHO" not in str(caught.value)
