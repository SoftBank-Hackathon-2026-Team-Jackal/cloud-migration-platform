"""Ticket 47 (#182): platform ECR for other accounts' EKS nodes (B-08) and pull-only tokens for
K3s/GKE (B-09, 41 D4), plus no global worker AWS profile (B-20).

Static seams over `infra/demo` and the worker wiring; the stack change itself is only a change set
until approved (see the ticket).
"""

import subprocess
from pathlib import Path

from test_build_registry_runtime import _template

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra" / "demo"
PULL = {"ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"}


def _set(value):
    return {value} if isinstance(value, str) else set(value)


def test_repository_policy_lets_listed_accounts_node_roles_pull_only():
    template = _template("build-stack.yaml")
    assert template["Parameters"]["PullAccountIds"]["Type"] == "CommaDelimitedList"
    assert template["Parameters"]["PullAccountIds"]["Default"] == ""
    policy = template["Resources"]["BuildRepository"]["Properties"]["RepositoryPolicyText"]
    when, document, otherwise = policy["If"]
    assert when == "HasPullAccounts" and otherwise == {"Ref": "AWS::NoValue"}
    (statement,) = document["Statement"]
    assert statement["Effect"] == "Allow" and statement["Principal"] == "*"
    assert _set(statement["Action"]) == PULL
    assert statement["Condition"] == {
        "StringEquals": {"aws:PrincipalAccount": {"Ref": "PullAccountIds"}},
        # infra/terraform/aws node role `${name}-eks-node`; connection clusters are `cmp-*`.
        "ArnLike": {"aws:PrincipalArn": "arn:aws:iam::*:role/cmp-*-eks-node"},
    }
    assert "HasPullAccounts" in template["Conditions"]


def test_pull_role_gets_a_registry_login_and_pulls_from_this_repository_only():
    resources = _template("build-stack.yaml")["Resources"]
    role = resources["EcrPullRole"]
    assert role["Type"] == "AWS::IAM::Role"
    props = role["Properties"]
    assert props["RoleName"] == "cmp-demo-ecr-pull"
    (trust,) = props["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {
        "AWS": {"Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${InstanceRoleName}"}
    }
    (policy,) = props["Policies"]
    statements = {s["Sid"]: s for s in policy["PolicyDocument"]["Statement"]}
    assert set(statements) == {"EcrLogin", "EcrPullOnly"}
    assert _set(statements["EcrLogin"]["Action"]) == {"ecr:GetAuthorizationToken"}
    assert _set(statements["EcrPullOnly"]["Action"]) == PULL
    assert statements["EcrPullOnly"]["Resource"] == {"GetAtt": "BuildRepository.Arn"}
    allowed = {a for s in statements.values() for a in _set(s["Action"])}
    assert not any(a.startswith(("ecr:Put", "ecr:Upload", "ecr:Initiate", "ecr:Complete"))
                   for a in allowed)  # fmt: skip


def test_instance_role_may_assume_only_the_pull_role_for_tokens():
    policy = _template("build-stack.yaml")["Resources"]["BuildAccessPolicy"]
    statements = {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}
    assume = statements["AssumeEcrPullRole"]
    assert assume["Action"] == "sts:AssumeRole"
    assert assume["Resource"] == {"GetAtt": "EcrPullRole.Arn"}


def _run_deploy(tmp_path, *, env_ids=None):
    """Run deploy-build-stack.sh against a fake `aws`/`git` and return the calls and output."""
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    calls = tmp_path / "calls"
    (tools / "git").write_text("#!/bin/sh\nexit 0\n")
    (tools / "aws").write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {calls}\n'
        'case "$*" in\n'
        "  *get-caller-identity*) echo 714066696240 ;;\n"
        "  *describe-stack-resource*) echo cmp-demo-control-plane-InstanceRole-ABC ;;\n"
        "  *DeployBucket*) echo cmp-demo-714066696240-apne2-deploy ;;\n"
        "  *RepositoryUri*) echo 714066696240.dkr.ecr.ap-northeast-2.amazonaws.com/cmp-platform-builds ;;\n"
        "  *PullRoleArn*) echo arn:aws:iam::714066696240:role/cmp-demo-ecr-pull ;;\n"
        "esac\n"
    )
    for tool in tools.iterdir():
        tool.chmod(0o755)
    env = {"PATH": f"{tools}:/usr/bin:/bin"}
    if env_ids is not None:
        env["CMP_ECR_PULL_ACCOUNT_IDS"] = env_ids
    result = subprocess.run(
        ["bash", str(DEMO / "deploy-build-stack.sh")],
        capture_output=True, text=True, env=env, check=False,
    )  # fmt: skip
    return result, calls.read_text() if calls.exists() else ""


def test_deploy_keeps_the_account_list_unless_given_and_prints_the_pull_profile(tmp_path):
    result, calls = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "PullAccountIds" not in calls  # CloudFormation keeps the previous value
    assert "PLATFORM_ECR_PULL_ROLE_ARN=arn:aws:iam::714066696240:role/cmp-demo-ecr-pull" in (
        result.stdout
    )
    assert "PLATFORM_ECR_AWS_PROFILE=cmp-demo-ecr-pull" in result.stdout


def test_deploy_passes_a_valid_account_list_and_refuses_a_malformed_one(tmp_path):
    result, calls = _run_deploy(tmp_path / "ok", env_ids="111122223333,444455556666")
    assert result.returncode == 0, result.stderr
    assert "PullAccountIds=111122223333,444455556666" in calls
    result, calls = _run_deploy(tmp_path / "bad", env_ids="1111,*")
    assert result.returncode != 0 and "cloudformation deploy" not in calls


def _secrets(tmp_path, env_lines: str) -> str:
    env = tmp_path / ".env"
    env.write_text(env_lines)
    subprocess.run(
        ["bash", str(DEMO / "ensure-secrets.sh"), str(env), str(tmp_path / "secrets")],
        check=True, capture_output=True,
    )  # fmt: skip
    return (tmp_path / "secrets/aws/config").read_text()


def test_pull_profile_assumes_the_pull_role_from_the_instance_role(tmp_path):
    arn = "arn:aws:iam::714066696240:role/cmp-demo-ecr-pull"
    config = _secrets(tmp_path, f"PLATFORM_ECR_PULL_ROLE_ARN={arn}\n")
    block = config.split("[profile cmp-demo-ecr-pull]\n", 1)[1].split("[", 1)[0]
    assert f"role_arn = {arn}\n" in block
    assert "credential_source = Ec2InstanceMetadata\n" in block
    assert "source_profile" not in block


def test_no_pull_profile_until_the_role_exists(tmp_path):
    assert "cmp-demo-ecr-pull" not in _secrets(tmp_path, "")


def test_api_and_worker_mint_with_the_same_configurable_pull_profile():
    from test_demo_runtime import _env

    expected = "${PLATFORM_ECR_AWS_PROFILE:-cmp-demo-build}"
    assert _env("api")["PLATFORM_ECR_AWS_PROFILE"] == expected
    assert _env("worker")["PLATFORM_ECR_AWS_PROFILE"] == expected


def test_worker_has_no_global_aws_profile():
    from test_demo_runtime import _env

    worker = _env("worker")
    assert "AWS_PROFILE" not in worker
    assert worker["BUILD_AWS_PROFILE"] == "cmp-demo-build"


def test_gke_pull_secret_tokens_use_the_pull_profile(monkeypatch):
    from cmp_worker.activities import gcp_workloads_cli

    monkeypatch.setenv("PROVISIONING_AWS_PROFILE", "cmp-demo-provisioning")
    monkeypatch.setenv("PLATFORM_ECR_AWS_PROFILE", "cmp-demo-ecr-pull")
    activities = gcp_workloads_cli.build_gcp_workloads_activities(None)
    assert activities._runtime._env["AWS_PROFILE"] == "cmp-demo-ecr-pull"
    monkeypatch.delenv("PLATFORM_ECR_AWS_PROFILE")
    activities = gcp_workloads_cli.build_gcp_workloads_activities(None)
    assert activities._runtime._env["AWS_PROFILE"] == "cmp-demo-provisioning"
