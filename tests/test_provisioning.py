import json
from pathlib import Path

import pytest
from cmp_worker.provisioning import prepare_plan

FIXTURES = Path(__file__).parent / "fixtures" / "provisioning"


def request():
    return json.loads((FIXTURES / "aws.json").read_text())


def test_aws_plan_keeps_input_lineage_and_separates_node_disk_from_db_pvc():
    result = prepare_plan(request())
    assert result.spec.plan.plan_id == "plan-001"
    assert result.spec.inputs.sandbox_ref.id == "sandbox-001"
    assert result.configuration.cluster_kind == "EKS"
    assert result.configuration.node_disk_gib == 60
    assert result.configuration.database_pvc_gib == 20
    assert result.configuration.database_workload == "StatefulSet"
    assert result.configuration.application_port == 8080
    assert result.configuration.image_uri.endswith("c" * 64)
    assert result.configuration.ingress_kind == "ALB"
    assert "object_storage" not in result.spec.model_dump()


def test_build_from_unvalidated_source_is_rejected_with_origin():
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = request()
    payload["build"]["source"]["sha256"] = "d" * 64
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan(payload)
    assert any(
        issue.path == "build.source" and issue.code == "LINEAGE_MISMATCH"
        for issue in caught.value.issues
    )


@pytest.mark.parametrize(
    "provider,cluster,network,ingress",
    [
        ("gcp", "GKE", "GCP_VPC", "GKE_INGRESS"),
        ("on_prem", "K3s", "EXISTING_K3S", "EXISTING_K3S"),
    ],
)
def test_other_providers_keep_same_workloads_with_distinct_infrastructure(
    provider, cluster, network, ingress
):
    payload = json.loads((FIXTURES / f"{provider}.json").read_text())
    result = prepare_plan(payload)
    assert result.spec.requirements.model_dump() == request()["analysis"]["requirements"]
    assert result.configuration.cluster_kind == cluster
    assert result.configuration.network_kind == network
    assert result.configuration.ingress_kind == ingress
    assert result.configuration.database_pvc_gib == 20
    assert result.configuration.execution_performed is False
    if provider == "on_prem":
        assert result.configuration.node_type is None
        assert result.spec.target.cluster_ref.id == "k3s-001"
    else:
        assert result.spec.target.network.vpc_cidr is None


def test_overlapping_subnets_are_rejected():
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = request()
    payload["target_selection"]["target"]["network"]["subnets"][1]["cidr"] = "10.20.1.128/25"
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan(payload)
    assert any(issue.code == "CIDR_OVERLAP" for issue in caught.value.issues)


def test_incompatible_node_architecture_is_rejected():
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = request()
    payload["target_selection"]["target"]["nodes"]["architecture"] = "linux/arm64"
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan(payload)
    assert any(issue.code == "ARCHITECTURE_MISMATCH" for issue in caught.value.issues)


def set_path(payload, path, value):
    parts = path.split(".")
    current = payload
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    key = int(parts[-1]) if isinstance(current, list) else parts[-1]
    if value is None:
        del current[key]
    else:
        current[key] = value


@pytest.mark.parametrize(
    "path,value,code",
    [
        ("contract_version", "2", "INVALID_INPUT"),
        ("analysis", None, "INVALID_INPUT"),
        ("sandbox", None, "INVALID_INPUT"),
        ("build", None, "INVALID_INPUT"),
        ("target_selection.target.provider", "AZURE", "INVALID_INPUT"),
        ("target_selection.target.credential_ref", None, "INVALID_INPUT"),
        ("target_selection.target.region", None, "INVALID_INPUT"),
        ("target_selection.target.nodes.count", 0, "INVALID_INPUT"),
        ("target_selection.target.nodes.count", True, "INVALID_INPUT"),
        ("analysis.migration_id", "other", "LINEAGE_MISMATCH"),
        ("sandbox.migration_id", "other", "LINEAGE_MISMATCH"),
        ("build.migration_id", "other", "LINEAGE_MISMATCH"),
        ("sandbox.analysis_ref.version", "2", "LINEAGE_MISMATCH"),
        ("sandbox.requirements_ref.version", "2", "LINEAGE_MISMATCH"),
        ("sandbox.source.version", "2", "LINEAGE_MISMATCH"),
        ("sandbox.config.sha256", "d" * 64, "LINEAGE_MISMATCH"),
        ("build.config.sha256", "d" * 64, "LINEAGE_MISMATCH"),
        ("build.sandbox_ref.version", "2", "LINEAGE_MISMATCH"),
        ("environment_ref.version", "2", "LINEAGE_MISMATCH"),
        ("target_selection.ref.version", "2", "LINEAGE_MISMATCH"),
        ("sandbox.status", "FAILED", "PRECONDITION_FAILED"),
        ("build.status", "FAILED", "PRECONDITION_FAILED"),
        ("build.image.uri", "registry.example.com/demo:latest", "INVALID_INPUT"),
        ("build.image.platforms", [], "INVALID_INPUT"),
        ("analysis.requirements.database.version", "15", "INVALID_INPUT"),
        ("target_selection.target.network.vpc_cidr", None, "MISSING_CIDR"),
        ("target_selection.target.network.mode", "EXISTING", "MISSING_NETWORK_REF"),
        ("target_selection.target.network.subnets.0.cidr", "10.20.1.1/24", "INVALID_CIDR"),
        ("target_selection.target.network.subnets.0.cidr", "not-a-cidr", "INVALID_CIDR"),
        ("target_selection.target.network.subnets.0.cidr", "2001:db8::/64", "INVALID_CIDR"),
        ("target_selection.target.network.subnets.0.cidr", "10.99.0.0/24", "CIDR_OUTSIDE_VPC"),
        ("target_selection.target.network.existing_cidrs", ["10.20.1.0/24"], "CIDR_OVERLAP"),
        ("target_selection.target.network.subnets.0.zone", "us-east-1a", "INVALID_TOPOLOGY"),
    ],
)
def test_invalid_or_stale_input_cannot_produce_a_plan(path, value, code):
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = request()
    set_path(payload, path, value)
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan(payload)
    assert code in {issue.code for issue in caught.value.issues}
    assert all(issue.path and issue.message for issue in caught.value.issues)


@pytest.mark.parametrize(
    "provider,field",
    [
        ("gcp", "credential_ref"),
        ("gcp", "region"),
        ("on_prem", "agent_id"),
        ("on_prem", "cluster_ref"),
        ("on_prem", "namespace"),
        ("on_prem", "storage_class"),
    ],
)
def test_other_providers_require_their_own_inputs(provider, field):
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = json.loads((FIXTURES / f"{provider}.json").read_text())
    del payload["target_selection"]["target"][field]
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan(payload)
    assert any(issue.path.endswith(field) for issue in caught.value.issues)


def test_existing_network_is_preserved_without_claiming_ownership():
    payload = request()
    network = payload["target_selection"]["target"]["network"]
    network["mode"] = "EXISTING"
    network["network_ref"] = {"id": "existing-vpc-001", "version": "1"}
    result = prepare_plan(payload)
    assert result.spec.target.network.mode == "EXISTING"
    assert result.spec.target.network.network_ref.id == "existing-vpc-001"


def test_gcp_rejects_aws_topology():
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = json.loads((FIXTURES / "gcp.json").read_text())
    network = payload["target_selection"]["target"]["network"]
    network["vpc_cidr"] = "10.30.0.0/16"
    network["subnets"][0]["zone"] = "asia-northeast3-a"
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan(payload)
    assert {issue.code for issue in caught.value.issues} == {
        "UNSUPPORTED_FIELD",
        "INVALID_TOPOLOGY",
    }


def test_errors_do_not_expose_rejected_secret_values():
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = request()
    payload["password"] = "do-not-print-this-secret"
    payload["build"]["image"]["uri"] = "do-not-print-this-secret"
    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan(payload)
    assert "do-not-print-this-secret" not in str(caught.value)
    assert "do-not-print-this-secret" not in repr(caught.value.issues)


def test_p0_object_storage_request_is_not_silently_accepted():
    from cmp_contracts.provisioning import ProvisioningInputError

    payload = request()
    payload["analysis"]["requirements"]["object_storage"] = {"bucket": "demo"}
    with pytest.raises(ProvisioningInputError):
        prepare_plan(payload)


def test_direct_adapter_also_rejects_incompatible_image():
    from cmp_contracts.provisioning import ProvisioningInputError, ProvisioningSpec
    from cmp_providers.provisioning import adapt

    spec_data = prepare_plan(request()).spec.model_dump()
    spec_data["image"]["platforms"] = ["linux/arm64"]
    with pytest.raises(ProvisioningInputError):
        adapt(ProvisioningSpec.model_validate(spec_data))


def test_cleanup_failure_is_reported_as_pending_policy_without_inventing_a_deployment_gate():
    payload = request()
    payload["sandbox"]["cleanup_status"] = "FAILED"
    result = prepare_plan(payload)
    assert result.spec.inputs.sandbox_cleanup_status == "FAILED"
    assert [issue.code for issue in result.pending_decisions] == ["CLEANUP_POLICY_REQUIRED"]
    assert result.configuration.execution_performed is False
