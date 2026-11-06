"""Data migration ticket 12: source EKS write access for the data Activities.

Static seams over `infra/demo` (template, RBAC manifest, ensure-secrets profile). The stack itself
is checked against the real cluster with `kubectl auth can-i` as the role (see the ticket).
"""

import subprocess
from pathlib import Path

import yaml
from test_build_registry_runtime import _template

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra" / "demo"
ROLE = "arn:aws:iam::714066696240:role/cmp-demo-data-migration"


def _resources() -> dict:
    return _template("data-migration-stack.yaml")["Resources"]


def test_stack_creates_only_the_role_access_entry_and_assume_policy():
    assert {k: v["Type"] for k, v in _resources().items()} == {
        "DataMigrationRole": "AWS::IAM::Role",
        "DataMigrationAccessEntry": "AWS::EKS::AccessEntry",
        "AssumeDataMigrationPolicy": "AWS::IAM::RolePolicy",
    }


def test_role_only_describes_the_source_cluster():
    props = _resources()["DataMigrationRole"]["Properties"]
    assert props["RoleName"] == "cmp-demo-data-migration"
    (policy,) = props["Policies"]
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert statement["Action"] == "eks:DescribeCluster"
    assert "cluster/${ClusterName}" in statement["Resource"]["Sub"]
    trust = props["AssumeRolePolicyDocument"]["Statement"]
    assert "${InstanceRoleName}" in trust[0]["Principal"]["AWS"]["Sub"]


def test_access_entry_is_edit_in_the_two_data_namespaces_only():
    props = _resources()["DataMigrationAccessEntry"]["Properties"]
    (access,) = props["AccessPolicies"]
    assert access["PolicyArn"].endswith("/AmazonEKSEditPolicy")
    assert access["AccessScope"]["Type"] == "namespace"
    assert access["AccessScope"]["Namespaces"] == [
        {"Ref": "AppNamespace"},
        {"Ref": "TunnelNamespace"},
    ]
    params = _template("data-migration-stack.yaml")["Parameters"]
    assert params["AppNamespace"]["Default"] == "chat-source"
    assert params["TunnelNamespace"]["Default"] == "cmp-data-tunnel"
    assert params["VerifierPrincipalArn"]["Default"] == ""


def test_instance_role_may_assume_only_the_data_role():
    props = _resources()["AssumeDataMigrationPolicy"]["Properties"]
    (statement,) = props["PolicyDocument"]["Statement"]
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Resource"] == {"GetAtt": "DataMigrationRole.Arn"}


def test_rbac_grants_only_get_patch_on_the_tunnel_namespace():
    from cmp_data import tunnel

    docs = {
        d["kind"]: d for d in yaml.safe_load_all((DEMO / "data-migration-rbac.yaml").read_text())
    }
    assert docs["Namespace"]["metadata"] == tunnel.namespace_manifest()["metadata"]
    (rule,) = docs["ClusterRole"]["rules"]
    assert rule == {"apiGroups": [""], "resources": ["namespaces"],
                    "resourceNames": [tunnel.NAMESPACE], "verbs": ["get", "patch"]}  # fmt: skip
    assert docs["ClusterRoleBinding"]["subjects"][0]["name"] == "cmp-data-migration"


def _secrets(tmp_path, env_lines: str) -> str:
    env = tmp_path / ".env"
    env.write_text(env_lines)
    subprocess.run(
        ["bash", str(DEMO / "ensure-secrets.sh"), str(env), str(tmp_path / "secrets")],
        check=True, capture_output=True,
    )  # fmt: skip
    return (tmp_path / "secrets/aws/config").read_text()


def test_data_profile_assumes_the_role_from_the_instance_role(tmp_path):
    config = _secrets(tmp_path, f"DATA_MIGRATION_ROLE_ARN={ROLE}\n")
    block = config.split("[profile cmp-demo-data-migration]\n", 1)[1].split("[", 1)[0]
    assert f"role_arn = {ROLE}\n" in block
    assert "credential_source = Ec2InstanceMetadata\n" in block
    assert "source_profile" not in block


def test_no_data_profile_until_the_role_is_set(tmp_path):
    assert "cmp-demo-data-migration" not in _secrets(tmp_path, "")
