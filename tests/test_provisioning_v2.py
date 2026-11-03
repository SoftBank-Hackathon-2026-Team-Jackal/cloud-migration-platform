"""Public v2 Planning boundary: resolved bytes plus upstream result references."""

import hashlib
import json

import pytest
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_worker.provisioning_v2 import prepare_plan_v2

SOURCE_SHA = "a" * 64
IMAGE_DIGEST = "sha256:" + "b" * 64
REQ_REF = {"id": "requirements-1", "version": "1"}


def _ref(name):
    return {"id": name, "version": "1"}


def _content_ref(name, sha="c" * 64):
    return {**_ref(name), "sha256": sha}


def _source(sha=SOURCE_SHA):
    return {
        "uri": f"s3://source-bucket/sources/{sha}.tar.gz",
        "version_id": "s3-v1",
        "sha256": sha,
    }


def _requirements():
    return {
        "contract_version": "2",
        "resources": [
            {
                "id": "postgres-main",
                "kind": "POSTGRESQL",
                "version": "16",
                "extensions": [],
                "database": {"name": "appdb", "username": "appuser"},
                "cpu_millicores": 250,
                "memory_mib": 512,
                "storage_gib": 5,
            },
            {
                "id": "api-main",
                "kind": "APPLICATION",
                "replicas": 1,
                "cpu_millicores": 250,
                "memory_mib": 256,
                "port": 8080,
                "health_path": "/health",
                "env_bindings": {
                    "DB_HOST": {"resource_id": "postgres-main", "output": "host"},
                    "DB_PASSWORD": {
                        "resource_id": "postgres-main",
                        "output": "password_secret_ref",
                    },
                },
            },
        ],
        "network_intent": [
            {
                "source_resource_id": "api-main",
                "destination_resource_id": "postgres-main",
                "port": 5432,
                "protocol": "TCP",
            }
        ],
    }


def _build(resource_id="api-main", sha=SOURCE_SHA, digest=IMAGE_DIGEST):
    return {
        "resource_id": resource_id,
        "result_ref": _ref(f"build-{resource_id}"),
        "status": "SUCCEEDED",
        "source_ref": _source(sha),
        "image_uri": f"123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/demo@{digest}",
        "image_digest": digest,
        "platforms": ["linux/amd64"],
    }


def _validated(resource_id="api-main", sha=SOURCE_SHA, digest=IMAGE_DIGEST):
    return {
        "resource_id": resource_id,
        "build_ref": _ref(f"build-{resource_id}"),
        "image_digest": digest,
        "platform": "linux/amd64",
        "source_sha256": sha,
    }


def _request(requirements=None):
    requirements = requirements or _requirements()
    content = json.dumps(requirements, sort_keys=True).encode()
    sha = hashlib.sha256(content).hexdigest()
    return (
        {
            "contract_version": "2",
            "migration_id": "migration-1",
            "analysis": {
                "result_ref": _ref("analysis-1"),
                "requirements_ref": _content_ref("requirements-1", sha),
                "source_refs": {"api-main": _source()},
            },
            "builds": [_build()],
            "sandbox": {
                "result_ref": _ref("sandbox-1"),
                "status": "PASSED",
                "validated_workloads": [_validated()],
                "requirements_ref": _content_ref("requirements-1", sha),
                "report_ref": _content_ref("report-1"),
                "test_suite_ref": _source("d" * 64),
                "cleanup_status": "CLEANED",
            },
            "target_selection": {
                "ref": _ref("target-1"),
                "provider": "ON_PREM",
                "location": None,
                "access_ref": _ref("cluster-1"),
                "capabilities_ref": _ref("capabilities-1"),
            },
            "environment": {
                "env_file_ref": _content_ref("env-1"),
                "mapping_ref": _content_ref("mapping-1"),
            },
            "policy": {"template_version": "helm-v2"},
        },
        content,
    )


def _codes(error):
    return {(issue.code, issue.path) for issue in error.value.issues}


def test_v2_planning_preserves_validated_image_and_defers_plan_identity():
    payload, content = _request()
    result = prepare_plan_v2(payload, requirements_bytes=content)
    assert result.spec.contract_version == "2"
    assert [resource.id for resource in result.spec.requirements.resources] == [
        "postgres-main",
        "api-main",
    ]
    assert result.spec.images[0].resource_id == "api-main"
    assert result.spec.images[0].image_uri.endswith(IMAGE_DIGEST)
    assert result.spec.images[0].build_ref.id == "build-api-main"
    assert result.spec.target_selection.capabilities_ref.id == "capabilities-1"
    assert result.spec.sandbox_report_ref.id == "report-1"
    assert result.spec.test_suite_ref.sha256 == "d" * 64
    assert "plan" not in result.spec.model_dump()
    assert result.pending_decisions == []


def test_multiple_applications_are_matched_by_resource_id():
    requirements = _requirements()
    second = requirements["resources"][1].copy()
    second["id"] = "worker-main"
    requirements["resources"].append(second)
    payload, content = _request(requirements)
    payload["analysis"]["source_refs"]["worker-main"] = _source("e" * 64)
    payload["builds"].append(_build("worker-main", "e" * 64, "sha256:" + "f" * 64))
    payload["sandbox"]["validated_workloads"].append(
        _validated("worker-main", "e" * 64, "sha256:" + "f" * 64)
    )
    result = prepare_plan_v2(payload, requirements_bytes=content)
    assert {image.resource_id for image in result.spec.images} == {"api-main", "worker-main"}


@pytest.mark.parametrize("provider", ["AWS", "GCP", "ON_PREM"])
def test_target_choice_does_not_require_a_profile(provider):
    payload, content = _request()
    payload["target_selection"]["provider"] = provider
    payload["target_selection"]["location"] = (
        "ap-northeast-2" if provider == "AWS" else "asia-northeast3" if provider == "GCP" else None
    )
    assert (
        prepare_plan_v2(payload, requirements_bytes=content).spec.target_selection.provider
        == provider
    )


@pytest.mark.parametrize(
    "change,code",
    [
        (
            lambda p: p["builds"][0].update(image_digest="sha256:" + "f" * 64),
            "IMAGE_DIGEST_MISMATCH",
        ),
        (lambda p: p["builds"][0]["source_ref"].update(version_id="s3-v2"), "SOURCE_MISMATCH"),
        (
            lambda p: p["sandbox"]["validated_workloads"][0]["build_ref"].update(version="2"),
            "BUILD_REF_MISMATCH",
        ),
        (
            lambda p: p["sandbox"]["validated_workloads"][0].update(
                image_digest="sha256:" + "f" * 64
            ),
            "IMAGE_DIGEST_MISMATCH",
        ),
        (
            lambda p: p["sandbox"]["validated_workloads"][0].update(source_sha256="f" * 64),
            "SOURCE_MISMATCH",
        ),
        (
            lambda p: p["sandbox"]["validated_workloads"][0].update(platform="linux/arm64"),
            "PLATFORM_MISMATCH",
        ),
        (lambda p: p["sandbox"]["requirements_ref"].update(version="2"), "REQUIREMENTS_MISMATCH"),
        (lambda p: p["builds"][0].update(status="FAILED"), "PRECONDITION_FAILED"),
        (lambda p: p["sandbox"].update(status="FAILED"), "PRECONDITION_FAILED"),
        (lambda p: p["builds"].clear(), "MISSING_BUILD"),
        (lambda p: p["sandbox"]["validated_workloads"].clear(), "MISSING_SANDBOX_VALIDATION"),
        (lambda p: p["builds"].append(_build()), "DUPLICATE_BUILD"),
    ],
)
def test_mismatched_evidence_cannot_be_planned(change, code):
    payload, content = _request()
    change(payload)
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content)
    assert code in {issue.code for issue in caught.value.issues}


def test_requirement_bytes_are_checked_against_ref_before_parsing():
    payload, content = _request()
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content + b" ")
    assert ("ARTIFACT_DIGEST_MISMATCH", "analysis.requirements_ref.sha256") in _codes(caught)


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda r: r["resources"][1].update(id="postgres-main"), "DUPLICATE_RESOURCE_ID"),
        (
            lambda r: r["resources"][1]["env_bindings"]["DB_HOST"].update(resource_id="missing"),
            "UNKNOWN_RESOURCE_REF",
        ),
        (lambda r: r["resources"][1].update(kind="REDIS"), "INVALID_REQUIREMENTS"),
        (lambda r: r["resources"][0].update(extensions=["postgis"]), "UNSUPPORTED_DB_EXTENSION"),
    ],
)
def test_invalid_resource_graph_is_rejected(change, code):
    requirements = _requirements()
    change(requirements)
    payload, content = _request(requirements)
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content)
    assert code in {issue.code for issue in caught.value.issues}


def test_separate_config_artifact_must_be_in_sandbox_evidence():
    payload, content = _request()
    payload["analysis"]["config_artifact_ref"] = _content_ref("config-1")
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content)
    assert "CONFIG_MISMATCH" in {issue.code for issue in caught.value.issues}
    payload["sandbox"]["config_artifact_ref"] = _content_ref("config-1")
    prepare_plan_v2(payload, requirements_bytes=content)


def test_cleanup_failure_is_explicit_pending_decision():
    payload, content = _request()
    payload["sandbox"]["cleanup_status"] = "FAILED"
    result = prepare_plan_v2(payload, requirements_bytes=content)
    assert [issue.code for issue in result.pending_decisions] == ["CLEANUP_POLICY_REQUIRED"]


def test_errors_never_echo_secret_input():
    payload, content = _request()
    payload["password"] = "do-not-print-this-secret"
    payload["builds"][0]["image_uri"] = "do-not-print-this-secret"
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content)
    assert "do-not-print-this-secret" not in str(caught.value)
    assert "do-not-print-this-secret" not in repr(caught.value.issues)


def test_schema_errors_never_echo_untrusted_mapping_keys():
    payload, content = _request()
    payload["analysis"]["source_refs"]["secret-bearing-key"] = _source()
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content)
    assert "secret-bearing-key" not in str(caught.value)
    assert "secret-bearing-key" not in repr(caught.value.issues)


def test_requirements_reject_invalid_environment_variable_name():
    requirements = _requirements()
    requirements["resources"][1]["env_bindings"]["invalid.env"] = requirements["resources"][1][
        "env_bindings"
    ].pop("DB_HOST")
    payload, content = _request(requirements)
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content)
    assert "INVALID_REQUIREMENTS" in {issue.code for issue in caught.value.issues}
