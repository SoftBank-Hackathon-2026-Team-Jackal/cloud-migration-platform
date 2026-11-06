"""Ticket 23b (#205): the demo's AWS connection Launch Stack link works.

Static seams over `infra/demo`: the api receives the two connection settings, the dedicated
template bucket stack makes only `connections/<version>/*` publicly readable, the platform
principal may assume only platform-tagged connection roles with an External ID, and the deploy
script (fake `aws`/`git`/`curl`) uploads both templates and prints the server `.env` lines. Real
stack apply, upload and a real verify are done on the demo account after approval.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra" / "demo"
CONNECTIONS = ROOT / "infra" / "connections"
ACCOUNT = "714066696240"
REGION = "ap-northeast-2"
BUCKET = f"cmp-demo-{ACCOUNT}-apne2-connection-templates"
BASE_URL = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/connections/v1"
ROLE = "cmp-demo-control-plane-InstanceRole-oAXRipePmZCe"
PRINCIPAL = f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"


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


def _template(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_CfnLoader)


def _bucket_stack() -> dict:
    return _template(DEMO / "connection-templates-stack.yaml")


def _actions(statement: dict) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


# ------------------------------------------------------------------------------ compose


def test_api_receives_the_principal_and_template_url_from_the_server_env():
    services = yaml.safe_load((DEMO / "compose.yaml").read_text())["services"]
    api = services["api"]["environment"]
    for name in ("CONNECTION_PLATFORM_PRINCIPAL_ARN", "CONNECTION_TEMPLATE_BASE_URL"):
        # Empty (unset in /opt/cmp/.env) keeps launch_stack_url null, the pre-23b behavior.
        assert api[name] == f"${{{name}:-}}", name
        # Only the API builds the link; the worker never reads these settings.
        assert name not in services["worker"]["environment"], name
    # The principal the link names is the identity the API assumes with.
    assert api["CONNECTION_PLATFORM_AWS_PROFILE"] == "cmp-demo-build"


def test_compose_values_reach_the_link_builder():
    from cmp_api.connections import EnvironmentConnection, launch_stack_url

    connection = EnvironmentConnection(
        id="conn-0123456789ab",
        kind="AWS_SOURCE",
        region=REGION,
        external_id="cmp-" + "a" * 32,
    )
    url = launch_stack_url(connection, template_base_url=BASE_URL, principal_arn=PRINCIPAL)
    assert url is not None
    assert "templateURL=" + BASE_URL.replace(":", "%3A").replace("/", "%2F") in url
    assert "source-role.yaml" in url
    assert launch_stack_url(connection, template_base_url="", principal_arn=PRINCIPAL) is None


# ------------------------------------------------------------------------------ bucket stack


def test_bucket_stack_creates_only_the_bucket_and_its_policy():
    resources = _bucket_stack()["Resources"]
    assert {k: v["Type"] for k, v in resources.items()} == {
        "TemplateBucket": "AWS::S3::Bucket",
        "TemplateBucketPolicy": "AWS::S3::BucketPolicy",
    }


def test_bucket_blocks_acls_owner_enforced_encrypted_versioned_and_named_for_the_account():
    bucket = _bucket_stack()["Resources"]["TemplateBucket"]
    props = bucket["Properties"]
    assert props["BucketName"] == {"Sub": "cmp-demo-${AWS::AccountId}-apne2-connection-templates"}
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        # Only the bucket policy below may make objects public.
        "BlockPublicPolicy": False,
        "RestrictPublicBuckets": False,
    }
    assert props["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    sse = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    # SSE-S3: anonymous readers cannot use a KMS key.
    assert sse["ServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    # Users' existing links keep pointing here; never delete it with the stack.
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"


def test_only_connection_template_objects_are_public_and_only_for_get():
    template = _bucket_stack()
    statements = template["Resources"]["TemplateBucketPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    public = [s for s in statements if s["Effect"] == "Allow"]
    assert public == [
        {
            "Sid": "PublicReadConnectionTemplates",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": {"Sub": "${TemplateBucket.Arn}/connections/${TemplateVersion}/*"},
        }
    ]
    version = template["Parameters"]["TemplateVersion"]
    assert version["Default"] == "v1"
    assert version["AllowedPattern"] == "^v[0-9]+$"
    denies = [s for s in statements if s["Effect"] == "Deny"]
    assert denies == [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [
                {"GetAtt": "TemplateBucket.Arn"},
                {"Sub": "${TemplateBucket.Arn}/*"},
            ],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }
    ]
    text = (DEMO / "connection-templates-stack.yaml").read_text()
    for forbidden in ("s3:ListBucket", "s3:PutObject", "s3:DeleteObject", "AccessControl"):
        assert forbidden not in text, forbidden


def test_bucket_stack_outputs_the_https_template_prefix():
    outputs = _bucket_stack()["Outputs"]
    assert outputs["TemplateBaseUrl"]["Value"] == {
        "Sub": "https://${TemplateBucket}.s3.${AWS::Region}.amazonaws.com/connections/${TemplateVersion}"
    }
    assert outputs["BucketName"]["Value"] == {"Ref": "TemplateBucket"}


# ------------------------------------------------------------------------------ AssumeRole


def _connection_policy() -> dict:
    return _template(DEMO / "build-stack.yaml")["Resources"]["ConnectionAssumePolicy"]


def test_platform_principal_may_assume_only_tagged_connection_roles_with_an_external_id():
    policy = _connection_policy()
    assert policy["Type"] == "AWS::IAM::RolePolicy"
    props = policy["Properties"]
    # Same existing instance role as the build policy; cmp-demo-control-plane stays untouched.
    assert props["RoleName"] == {"Ref": "InstanceRoleName"}
    assert props["PolicyName"] == "cmp-demo-connections"
    [statement] = props["PolicyDocument"]["Statement"]
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == {"sts:AssumeRole"}
    # CloudFormation names the user's role; the account is the user's.
    assert statement["Resource"] == {"Sub": "arn:${AWS::Partition}:iam::*:role/*"}
    assert statement["Condition"] == {
        "StringEquals": {"iam:ResourceTag/cmp:managed-by": "cloud-migration-platform"},
        "StringLike": {"sts:ExternalId": "cmp-*"},
        "Null": {"sts:ExternalId": "false"},
    }


@pytest.mark.parametrize("name", ["source-role.yaml", "target-role.yaml"])
def test_both_connection_templates_carry_the_tag_the_assume_condition_requires(name):
    role = _template(CONNECTIONS / name)["Resources"]["ConnectionRole"]["Properties"]
    tags = {t["Key"]: t["Value"] for t in role["Tags"]}
    assert tags["cmp:managed-by"] == "cloud-migration-platform"
    external_id = _template(CONNECTIONS / name)["Parameters"]["ExternalId"]
    assert external_id["AllowedPattern"].startswith("^cmp-")


# ------------------------------------------------------------------------------ deploy script


def _run_deploy(tmp_path, *args, existing: dict[str, str] | None = None, head_status="200"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    existing = existing or {}
    remote = tmp_path / "remote"
    remote.mkdir()
    for name, body in existing.items():
        (remote / name).write_text(body)
    stubs = {
        "git": "exit 0\n",  # `git status --porcelain`: clean tree
        "aws": f"""echo "aws $*" >>{log}
case "$*" in
  *get-caller-identity*) echo {ACCOUNT} ;;
  *describe-stack-resource*) echo {ROLE} ;;
  *"iam get-role"*) echo {PRINCIPAL} ;;
  *OutputKey==\\`BucketName\\`*) echo {BUCKET} ;;
  *TemplateBaseUrl*) echo {BASE_URL} ;;
  *"cloudformation deploy"*) ;;
  *"s3 cp"*) ;;
  *) echo "unexpected aws $*" >&2; exit 9 ;;
esac
""",
        "curl": f"""echo "curl $*" >>{log}
url="${{@: -1}}"
name="${{url##*/}}"
case "$*" in
  *-sI*) echo "HTTP/1.1 {head_status} OK" ;;
  *) out=""
     while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done
     [[ -f "{remote}/$name" ]] || exit 22
     cp "{remote}/$name" "$out" ;;
esac
""",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", str(DEMO / "deploy-connection-templates.sh"), *args],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_plan_creates_a_change_set_only(tmp_path):
    result, calls = _run_deploy(tmp_path, "--plan")
    assert result.returncode == 0, result.stderr
    [deploy] = [c for c in calls if "cloudformation deploy" in c]
    assert "--no-execute-changeset" in deploy
    assert "--stack-name cmp-demo-connection-templates" in deploy
    assert "--template-file infra/demo/connection-templates-stack.yaml" in deploy
    assert "TemplateVersion=v1" in deploy
    assert not [c for c in calls if "s3 cp" in c or c.startswith("curl")]
    assert "CONNECTION_TEMPLATE_BASE_URL=" not in result.stdout


def test_apply_uploads_both_templates_as_yaml_checks_them_and_prints_env_lines(tmp_path):
    result, calls = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    [deploy] = [c for c in calls if "cloudformation deploy" in c]
    assert "--no-execute-changeset" not in deploy
    uploads = [c for c in calls if "s3 cp" in c]
    assert len(uploads) == 2
    for name in ("source-role.yaml", "target-role.yaml"):
        [upload] = [c for c in uploads if f"infra/connections/{name}" in c]
        assert f"s3://{BUCKET}/connections/v1/{name}" in upload
        assert "--content-type text/yaml" in upload
        assert f"curl -sI {BASE_URL}/{name}" in calls
    lines = result.stdout.splitlines()
    assert f"CONNECTION_PLATFORM_PRINCIPAL_ARN={PRINCIPAL}" in lines
    assert f"CONNECTION_TEMPLATE_BASE_URL={BASE_URL}" in lines


def test_apply_skips_an_identical_template_and_refuses_a_changed_one(tmp_path):
    same = (CONNECTIONS / "source-role.yaml").read_text()
    result, calls = _run_deploy(tmp_path, existing={"source-role.yaml": same})
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if "s3 cp" in c and "source-role.yaml" in c] == []

    changed = tmp_path / "changed"
    changed.mkdir()
    result, calls = _run_deploy(changed, existing={"target-role.yaml": "changed: true\n"})
    assert result.returncode != 0
    assert "TemplateVersion" in result.stderr
    assert not [c for c in calls if "s3 cp" in c and "target-role.yaml" in c]
    assert "CONNECTION_TEMPLATE_BASE_URL=" not in result.stdout


def test_apply_fails_when_a_template_is_not_publicly_readable(tmp_path):
    result, _ = _run_deploy(tmp_path, head_status="403")
    assert result.returncode != 0
    assert "CONNECTION_TEMPLATE_BASE_URL=" not in result.stdout


def test_script_refuses_an_unknown_mode_and_never_touches_the_control_plane_stack():
    script = (DEMO / "deploy-connection-templates.sh").read_text()
    assert "cloudformation.yaml" not in script
    assert '--stack-name "$control_stack" --logical-resource-id InstanceRole' in script
    assert "profile=cmp-aws-prod" in script and f"account={ACCOUNT}" in script
