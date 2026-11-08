"""Ticket 28 (#152): build registry and Sandbox storage runtime on the demo host.

Static seams over `infra/demo`: the separate build stack (least privilege, immutable ECR, the key
prefixes the worker code actually writes), the untouched control-plane stack, the worker's Docker
socket and build settings, and the build profile. Real stack deploy and a real push are verified
on the demo host after approval (see the ticket).
"""

import inspect
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from cmp_worker import build_wiring, image_build, source_archive

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra" / "demo"


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


_CfnLoader.add_multi_constructor("!", _tag)


def _template(name: str) -> dict:
    return yaml.load((DEMO / name).read_text(), Loader=_CfnLoader)


def _statements() -> dict[str, dict]:
    policy = _template("build-stack.yaml")["Resources"]["BuildAccessPolicy"]
    return {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}


def _actions(statement: dict) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def test_build_stack_creates_only_the_repository_and_an_inline_policy():
    resources = _template("build-stack.yaml")["Resources"]
    assert {k: v["Type"] for k, v in resources.items()} == {
        "BuildRepository": "AWS::ECR::Repository",
        "BuildAccessPolicy": "AWS::IAM::RolePolicy",
        # Ticket 23b (#205): connection AssumeRole, a separate inline policy on the same role
        # (scope tested in tests/test_connection_launch_stack.py).
        "ConnectionAssumePolicy": "AWS::IAM::RolePolicy",
        # Ticket 47 (B-09): pull-only token role (tests/test_platform_ecr_access.py).
        "EcrPullRole": "AWS::IAM::Role",
    }
    # Attached to the existing role by parameter; no user or bucket is created here (47 adds the
    # pull-only role).
    for name in ("BuildAccessPolicy", "ConnectionAssumePolicy"):
        assert resources[name]["Properties"]["RoleName"] == {"Ref": "InstanceRoleName"}


def test_repository_is_immutable_scanned_retained_and_expires_untagged_only():
    template = _template("build-stack.yaml")
    repo = template["Resources"]["BuildRepository"]
    props = repo["Properties"]
    assert template["Parameters"]["RepositoryName"]["Default"] == "cmp-platform-builds"
    assert props["ImageTagMutability"] == "IMMUTABLE"
    assert props["ImageScanningConfiguration"] == {"ScanOnPush": True}
    assert repo["DeletionPolicy"] == repo["UpdateReplacePolicy"] == "Retain"
    lifecycle = props["LifecyclePolicy"]["LifecyclePolicyText"]["Sub"]
    assert '"tagStatus": "untagged"' in lifecycle
    assert '"tagStatus": "tagged"' not in lifecycle and "any" not in lifecycle


def test_ecr_permissions_are_scoped_to_the_repository_except_login():
    statements = _statements()
    assert statements["EcrLogin"]["Resource"] == "*"
    assert _actions(statements["EcrLogin"]) == {"ecr:GetAuthorizationToken"}
    repo_arn = {"GetAtt": "BuildRepository.Arn"}
    # Pull is also what ticket 29 mints a pull token for.
    assert {
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
    } == _actions(statements["EcrPull"])
    assert {
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
    } == _actions(statements["EcrPush"])
    for sid in ("EcrPull", "EcrPush"):
        assert statements[sid]["Resource"] == repo_arn, sid
    for sid, statement in statements.items():
        assert statement["Effect"] == "Allow"
        assert not any(a.endswith("*") for a in _actions(statement)), sid
        if sid != "EcrLogin":
            assert "*" not in str(statement["Resource"]).replace("/*", ""), sid


def test_s3_access_is_get_put_on_the_code_prefixes_and_never_releases():
    template = _template("build-stack.yaml")
    defaults = {
        k: template["Parameters"][k]["Default"]
        for k in ("SourcePrefix", "SuitePrefix", "LogPrefix")
    }
    assert defaults == {"SourcePrefix": "sources", "SuitePrefix": "tests", "LogPrefix": "builds"}
    # The defaults are the keys the worker code writes today.
    assert (
        inspect.signature(source_archive.package_source).parameters["prefix"].default == "sources"
    )
    assert inspect.signature(build_wiring.upload_suite).parameters["prefix"].default == "tests"
    assert '/builds/"' in inspect.getsource(image_build.ImageBuilder.build)

    statements = _statements()
    objects = statements["SandboxObjects"]
    assert _actions(objects) == {"s3:GetObject", "s3:PutObject"}
    assert [r["Sub"] for r in objects["Resource"]] == [
        "arn:${AWS::Partition}:s3:::${DeployBucketName}/${SourcePrefix}/*",
        "arn:${AWS::Partition}:s3:::${DeployBucketName}/${SuitePrefix}/*",
        "arn:${AWS::Partition}:s3:::${DeployBucketName}/${LogPrefix}/*",
    ]
    assert _actions(statements["SandboxList"]) == {"s3:ListBucket"}
    assert statements["SandboxList"]["Resource"] == {
        "Sub": "arn:${AWS::Partition}:s3:::${DeployBucketName}"
    }
    text = (DEMO / "build-stack.yaml").read_text()
    assert "s3:DeleteObject" not in text and "s3:*" not in text
    assert "releases/" not in str(statements)
    rules = template["Rules"]["ReleasesStayReadOnly"]["Assertions"]
    assert len(rules) == 4  # the three code prefixes and the red-team cache prefix


def test_red_team_cache_prefix_is_get_put_only_and_never_releases():
    """The worker's attack-result cache (`redteam_cache`) writes `redteam/attack/...` when
    `REDTEAM_CACHE_PREFIX` is empty, in the deploy bucket. Read and write that prefix only; the
    bucket listing the stack already grants is what turns a missing key into a clean 404."""
    from cmp_worker.redteam_cache import cache_key

    template = _template("build-stack.yaml")
    assert template["Parameters"]["RedteamCachePrefix"]["Default"] == "redteam"
    assert cache_key("a" * 64, []).startswith("redteam/")  # the key the default prefix covers
    statement = _statements()["RedteamCacheObjects"]
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == {"s3:GetObject", "s3:PutObject"}
    assert statement["Resource"] == {
        "Sub": "arn:${AWS::Partition}:s3:::${DeployBucketName}/${RedteamCachePrefix}/*"
    }
    rules = template["Rules"]["ReleasesStayReadOnly"]["Assertions"]
    assert any("RedteamCachePrefix" in str(rule["Assert"]) for rule in rules)
    # The existing code prefixes keep exactly their own three resources.
    assert len(_statements()["SandboxObjects"]["Resource"]) == 3


def test_control_plane_stack_is_unchanged_by_this_ticket():
    template = _template("cloudformation.yaml")
    text = (DEMO / "cloudformation.yaml").read_text()
    assert "ecr:" not in text and "AWS::ECR::" not in text
    role = template["Resources"]["InstanceRole"]["Properties"]
    assert [p["PolicyName"] for p in role["Policies"]] == ["cmp-demo-storage"]
    deploy = [
        s
        for s in role["Policies"][0]["PolicyDocument"]["Statement"]
        if "DeployBucket" in str(s["Resource"])
    ]
    assert deploy == [
        {
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": {"Sub": "${DeployBucket.Arn}/releases/*"},
        }
    ]
    deploy_script = (DEMO / "deploy-build-stack.sh").read_text()
    assert '--stack-name "$stack"' in deploy_script and "stack=cmp-demo-build" in deploy_script
    assert "--template-file infra/demo/build-stack.yaml" in deploy_script
    assert "cloudformation.yaml" not in deploy_script
    assert "--no-execute-changeset" in deploy_script


def test_only_the_worker_gets_the_docker_socket():
    services = yaml.safe_load((DEMO / "compose.yaml").read_text())["services"]
    holders = {
        name
        for name, service in services.items()
        for mount in service.get("volumes", [])
        if "docker.sock" in mount
    }
    assert holders == {"worker"}
    assert "/var/run/docker.sock:/var/run/docker.sock" in services["worker"]["volumes"]
    for name, service in services.items():
        assert not service.get("privileged"), name


def test_worker_build_settings_are_names_with_an_explicit_build_profile():
    env = yaml.safe_load((DEMO / "compose.yaml").read_text())["services"]["worker"]["environment"]
    for name in (
        "BUILD_ECR_REPOSITORY",
        "BUILD_SOURCE_BUCKET",
        "BUILD_LOG_BUCKET",
        "BUILD_PLATFORM",
    ):
        assert env[name] == f"${{{name}:-}}", name
    # Ticket 47 (B-20): the Build module passes this profile itself; no global AWS_PROFILE.
    assert env["BUILD_AWS_PROFILE"] == "cmp-demo-build" and "AWS_PROFILE" not in env
    assert env["PROVISIONING_AWS_PROFILE"] != "cmp-demo-build"
    # Empty settings keep the pre-built images instead of half-enabling the build.
    assert build_wiring.BuildSettings.from_values("", "", "", "", "cmp-demo-build") is None
    script = (DEMO / "deploy-build-stack.sh").read_text()
    for line in (
        "BUILD_ECR_REPOSITORY=$uri",
        "BUILD_SOURCE_BUCKET=$bucket",
        "BUILD_LOG_BUCKET=$bucket",
        "BUILD_PLATFORM=linux/amd64",
    ):
        assert line in script


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_build_profile_is_the_instance_role_even_when_provisioning_assumes_a_role(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PROVISIONING_AWS_ROLE_ARN=arn:aws:iam::1:role/x\n")
    secrets = tmp_path / "secrets"
    subprocess.run(
        ["bash", str(DEMO / "ensure-secrets.sh"), str(env_file), str(secrets)], check=True
    )
    config = (secrets / "aws" / "config").read_text()
    build = config.split("[profile cmp-demo-build]\n", 1)[1]
    assert build == "credential_source = Ec2InstanceMetadata\nregion = ap-northeast-2\n"
    assert config.count("role_arn") == 1
    assert "aws_access_key_id" not in config


def test_start_script_checks_the_worker_reaches_the_host_daemon():
    start = (DEMO / "start.sh").read_text()
    assert "docker version && docker buildx version" in start
