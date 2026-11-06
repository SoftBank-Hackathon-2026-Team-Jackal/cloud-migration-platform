"""Ticket 46 (#181): Provisioning runs with the selected connection's `credential_ref`.

Seams: the pure profile resolver, the 05 Plan and 08 Apply boundaries with fake `aws`/`terraform`
(arguments and `AWS_PROFILE` recorded), the Plan-time connection check, the 09 workload runtime
hook, the connections API (start-up config sync, platform principal) and the demo/connector
templates. No cloud call is made.
"""

import configparser
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from cmp_api import connections
from cmp_api.config import settings
from cmp_api.connections import ConnectionFailure, EnvironmentConnection, check_target_connection
from cmp_api.db import Base
from cmp_contracts.provisioning import ProvisioningInputError, VersionRef
from cmp_providers.aws_credentials import (
    AwsProfiles,
    connection_id_of,
    connection_profile,
    resolve_aws_profiles,
)
from cmp_providers.aws_preview import AwsPlanInput, plan_aws_infrastructure
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
CONN = "conn-0123456789ab"
PROFILE = "cmp-conn-0123456789ab"
PLATFORM = "cmp-demo-provisioning"


def ref(id_: str) -> VersionRef:
    return VersionRef(id=id_, version="1")


# --- resolver --------------------------------------------------------------------------------


def test_connection_ref_runs_the_target_with_the_connection_and_state_with_the_platform():
    profiles = resolve_aws_profiles(ref(CONN), platform_profile=PLATFORM)
    assert profiles == AwsProfiles(provider=PROFILE, state=PLATFORM, connection_id=CONN)
    assert connection_profile(CONN) == PROFILE
    assert connection_id_of(ref(CONN)) == CONN


def test_profile_name_ref_from_the_start_request_names_the_same_connection():
    """Ticket 44 writes the connection's `credential_ref` (`cmp-conn-<hex>`, ADR-005)."""
    profiles = resolve_aws_profiles(ref(PROFILE), platform_profile=PLATFORM)
    assert profiles == AwsProfiles(provider=PROFILE, state=PLATFORM, connection_id=CONN)
    assert connection_id_of(ref(PROFILE)) == CONN


def test_non_connection_ref_keeps_the_fixed_platform_profile_for_everything():
    profiles = resolve_aws_profiles(ref("credential-001"), platform_profile=PLATFORM)
    assert profiles == AwsProfiles(provider=PLATFORM, state=PLATFORM, connection_id=None)
    assert connection_id_of(ref("credential-001")) is None


@pytest.mark.parametrize(
    "bad", ["conn-", "conn-XYZ", "conn-0123456789a", "conn-../etc", "cmp-conn-x", "cmp-conn-"]
)
def test_malformed_connection_ref_is_refused(bad):
    with pytest.raises(ProvisioningInputError, match="INVALID_CREDENTIAL_REF"):
        resolve_aws_profiles(ref(bad), platform_profile=PLATFORM)


@pytest.mark.parametrize("platform", ["", "default", None])
def test_platform_profile_must_be_named(platform):
    with pytest.raises(ProvisioningInputError, match="MISSING_PROFILE"):
        resolve_aws_profiles(ref(CONN), platform_profile=platform)


def test_api_credential_ref_is_the_same_profile_name():
    connection = EnvironmentConnection(id=CONN, kind="AWS_TARGET", role_arn="arn:x")
    assert connections.credential_ref(connection) == PROFILE


# --- 05 Plan ---------------------------------------------------------------------------------


def plan_request(credential=CONN, **override) -> AwsPlanInput:
    from test_aws_preview import request

    data = request().model_dump(mode="json")
    data["credential_ref"] = {"id": credential, "version": "1"}
    data.update(override)
    return AwsPlanInput.model_validate(data)


def recording_tools(tmp_path: Path, monkeypatch, *, aws_rc=0, fail="") -> Path:
    """Fake `aws`/`terraform` that log `<tool> AWS_PROFILE=<env> <args>` and fail on demand."""
    from test_aws_preview import _fake_tools

    terraform, _ = _fake_tools(tmp_path, monkeypatch)
    log = tmp_path / "profiles"
    aws = tmp_path / "tools/aws"
    aws.write_text(
        "#!/bin/sh\n"
        f'printf \'aws AWS_PROFILE=%s %s\\n\' "$AWS_PROFILE" "$*" >> {log}\n'
        f"[ {aws_rc} -ne 0 ] && {{ echo NoCredentials-SECRET >&2; exit {aws_rc}; }}\n"
        "printf '%s' '{\"Account\":\"123456789012\"}'\n"
    )
    original = terraform.read_text().split("\n", 1)[1]
    terraform.write_text(
        "#!/bin/sh\n"
        f'printf \'terraform AWS_PROFILE=%s %s\\n\' "$AWS_PROFILE" "$*" >> {log}\n'
        + (f'if [ "$1" = {fail} ]; then exit 1; fi\n' if fail else "")
        + original
    )
    return terraform, log


def test_plan_uses_the_connection_profile_for_provider_and_platform_for_state(
    tmp_path, monkeypatch
):
    terraform, log = recording_tools(tmp_path, monkeypatch)
    plan_aws_infrastructure(
        plan_request(),
        output_dir=tmp_path / "plans",
        terraform_bin=str(terraform),
        aws_profile=PLATFORM,
    )
    lines = log.read_text().splitlines()
    identity = next(line for line in lines if " sts get-caller-identity" in line)
    assert identity.startswith(f"aws AWS_PROFILE={PROFILE} ") and f"--profile {PROFILE}" in identity
    assert all(f"AWS_PROFILE={PROFILE}" in line for line in lines)
    init = next(line for line in lines if " init " in line)
    assert f"-backend-config=profile={PLATFORM}" in init
    spec = json.loads((tmp_path / "plans/plan-001-r1/spec.json").read_text())
    assert spec["credential_ref"] == {"id": CONN, "version": "1"}  # pinned for Apply


def test_same_account_plan_keeps_one_platform_profile(tmp_path, monkeypatch):
    terraform, log = recording_tools(tmp_path, monkeypatch)
    plan_aws_infrastructure(
        plan_request("credential-001", cluster_name="demo"),  # no prefix rule without a connection
        output_dir=tmp_path / "plans",
        terraform_bin=str(terraform),
        aws_profile=PLATFORM,
    )
    lines = log.read_text().splitlines()
    assert all(f"AWS_PROFILE={PLATFORM}" in line for line in lines)
    assert f"-backend-config=profile={PLATFORM}" in next(l for l in lines if " init " in l)


def test_connection_plan_requires_the_template_iam_prefix(tmp_path, monkeypatch):
    terraform, log = recording_tools(tmp_path, monkeypatch)
    with pytest.raises(ProvisioningInputError, match="INVALID_CLUSTER_NAME"):
        plan_aws_infrastructure(
            plan_request(cluster_name="demo-target"),
            output_dir=tmp_path / "plans",
            terraform_bin=str(terraform),
            aws_profile=PLATFORM,
        )
    assert not log.exists()


def codes(error) -> list[str]:
    return [issue.code for issue in error.value.issues]


def test_identity_failure_is_its_own_code_with_the_step(tmp_path, monkeypatch):
    terraform, _ = recording_tools(tmp_path, monkeypatch, aws_rc=255)
    with pytest.raises(ProvisioningInputError) as error:
        plan_aws_infrastructure(
            plan_request(),
            output_dir=tmp_path / "plans",
            terraform_bin=str(terraform),
            aws_profile=PLATFORM,
        )
    assert codes(error) == ["AWS_IDENTITY_UNAVAILABLE", "PLAN_STEP_IDENTITY"]
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("step", ["init", "validate", "plan"])
def test_terraform_failure_records_the_step(tmp_path, monkeypatch, step):
    terraform, _ = recording_tools(tmp_path, monkeypatch, fail=step)
    with pytest.raises(ProvisioningInputError) as error:
        plan_aws_infrastructure(
            plan_request(),
            output_dir=tmp_path / "plans",
            terraform_bin=str(terraform),
            aws_profile=PLATFORM,
        )
    assert codes(error) == ["PLAN_COMMAND_FAILED", f"PLAN_STEP_{step.upper()}"]
    assert error.value.issues[1].path == f"terraform.{step}"


# --- 08 Apply --------------------------------------------------------------------------------


def apply_with(tmp_path, credential):
    from cmp_providers.aws_apply import apply_aws_infrastructure
    from test_aws_apply import FakeRunner, artifacts, spec

    class Recording(FakeRunner):
        def __init__(self):
            super().__init__()
            self.envs = []

        def __call__(self, args, cwd, env, timeout):
            self.envs.append((list(args), env.get("AWS_PROFILE")))
            return super().__call__(args, cwd, env, timeout)

    request = spec(credential_ref={"id": credential, "version": "1"})
    manifest, preview, directory = artifacts(tmp_path, request)
    runner = Recording()
    result = apply_aws_infrastructure(
        manifest, preview, directory, aws_profile=PLATFORM, terraform_bin="terraform", runner=runner
    )
    return result, runner


def test_apply_uses_the_connection_pinned_in_the_approved_spec(tmp_path):
    result, runner = apply_with(tmp_path, CONN)
    assert result["status"] == "SUCCEEDED", result
    assert {profile for _, profile in runner.envs} == {PROFILE}
    for args, _ in runner.envs:
        if args[0] == "aws":
            assert args[args.index("--profile") + 1] == PROFILE
    init = next(args for args, _ in runner.envs if args[:2] == ["terraform", "init"])
    assert f"-backend-config=profile={PLATFORM}" in init


def test_apply_without_a_connection_keeps_the_platform_profile(tmp_path):
    result, runner = apply_with(tmp_path, "cred-aws")
    assert result["status"] == "SUCCEEDED"
    assert {profile for _, profile in runner.envs} == {PLATFORM}


def test_apply_refuses_a_malformed_connection_ref_before_any_command(tmp_path):
    result, runner = apply_with(tmp_path, "conn-bad")
    assert (result["status"], result["code"]) == ("FAILED", "INVALID_CREDENTIAL_REF")
    assert runner.envs == []


# --- Plan-time connection check --------------------------------------------------------------


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def add_connection(session: Session, **override) -> EnvironmentConnection:
    values = {
        "id": CONN,
        "kind": "AWS_TARGET",
        "display_name": "target",
        "region": "ap-northeast-2",
        "external_id": "cmp-" + "a" * 32,
        "role_arn": "arn:aws:iam::123456789012:role/cmp-target-" + "a" * 32,
        "account_id": "123456789012",
        "status": "CONNECTED",
        "created_at": 1,
    }
    values.update(override)
    connection = EnvironmentConnection(**values)
    session.add(connection)
    session.commit()
    return connection


@pytest.mark.parametrize(
    ("override", "code"),
    [
        (None, "TARGET_CONNECTION_NOT_FOUND"),
        ({"kind": "AWS_SOURCE"}, "TARGET_CONNECTION_KIND_MISMATCH"),
        ({"status": "PENDING"}, "TARGET_CONNECTION_NOT_VERIFIED"),
        ({"role_arn": None}, "TARGET_CONNECTION_NOT_VERIFIED"),
        ({"account_id": "999999999999"}, "TARGET_CONNECTION_ACCOUNT_MISMATCH"),
    ],
)
def test_target_connection_must_be_a_verified_target_in_the_plan_account(sessions, override, code):
    with sessions() as session:
        if override is not None:
            add_connection(session, **override)
        with pytest.raises(ConnectionFailure, match=code):
            check_target_connection(session, CONN, account_id="123456789012")


def test_verified_target_connection_passes(sessions):
    with sessions() as session:
        add_connection(session)
        assert check_target_connection(session, CONN, account_id="123456789012").id == CONN


def test_publisher_refuses_an_unverified_connection_before_terraform(tmp_path, monkeypatch):
    from cmp_api.onprem_rollout import LocalArtifactStore
    from cmp_worker.plan_publish import PlanPublisher
    from test_plan_publish import MIGRATION, cloud_settings, register, registration

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)
    store = LocalArtifactStore(tmp_path / "store")
    monkeypatch.setattr(settings, "platform_ecr_registry", None)
    terraform, log = recording_tools(tmp_path, monkeypatch)
    target = {"provider": "AWS", "settings": cloud_settings(plan_request())}
    register(db, store, registration("AWS", target=target)[0])
    with db() as session:
        add_connection(session, status="FAILED")
    pub = PlanPublisher(
        db,
        store,
        cloud_artifact_root=tmp_path / "plans",
        aws_profile=PLATFORM,
        terraform_bin=str(terraform),
    )
    result = pub.publish(MIGRATION)
    assert (result["status"], result["code"]) == ("FAILED", "PLAN_FAILED")
    assert result["issues"] == ["TARGET_CONNECTION_NOT_VERIFIED"]
    assert not log.exists()
    with db() as session:
        session.get(EnvironmentConnection, CONN).status = "CONNECTED"
        session.commit()
    result = pub.publish(MIGRATION)
    assert result["status"] == "PUBLISHED", result
    assert f"AWS_PROFILE={PROFILE}" in log.read_text()
    engine.dispose()


# --- 09 AWS workloads ------------------------------------------------------------------------


def test_cli_runtime_with_profile_sets_only_the_subprocess_profile():
    from cmp_worker.activities.aws_workloads_cli import CliRuntime

    base = CliRuntime(aws_profile=PLATFORM, kubectl="k", helm="h", aws="a")
    other = base.with_profile(PROFILE)
    assert other is not base
    assert (other.kubectl, other.helm, other.aws) == ("k", "h", "a")
    assert other.aws_profile == PROFILE and other._env["AWS_PROFILE"] == PROFILE
    assert base._env["AWS_PROFILE"] == PLATFORM
    assert base.with_profile(PLATFORM) is base


async def test_workloads_connect_with_the_connection_from_the_approved_spec(tmp_path):
    from cmp_worker.activities.aws_workloads import AwsWorkloadsActivities
    from test_aws_workloads import (
        FAST,
        ClusterTarget,
        FakeCluster,
        FakeRuntime,
        FixedResolver,
        Plan,
        run,
    )

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)

    class ProfiledRuntime(FakeRuntime):
        def __init__(self, cluster, profile=PLATFORM, seen=None):
            super().__init__(cluster)
            self.aws_profile, self.seen = profile, seen if seen is not None else []

        def with_profile(self, profile):
            return (
                self
                if profile == self.aws_profile
                else ProfiledRuntime(self.cluster, profile, self.seen)
            )

        def account_id(self):
            self.seen.append(("account", self.aws_profile))
            return super().account_id()

        @contextmanager
        def connect(self, target: ClusterTarget):
            self.seen.append(("connect", self.aws_profile))
            with super().connect(target) as cluster:
                yield cluster

    spec = {
        "target": {"provider": "AWS", "settings": {"credential_ref": {"id": CONN, "version": "1"}}}
    }
    plan = Plan(tmp_path / "artifacts", db, spec=spec)
    runtime = ProfiledRuntime(FakeCluster())
    activities = AwsWorkloadsActivities(
        db, artifact_root=tmp_path / "artifacts", resolver=FixedResolver(), runtime=runtime,
        timeouts=FAST,
    )  # fmt: skip
    await run(activities, plan.request())
    assert runtime.seen[:2] == [("account", PROFILE), ("connect", PROFILE)]
    engine.dispose()


# --- connections API -------------------------------------------------------------------------


def test_api_start_up_rewrites_the_shared_config_from_the_db(sessions, tmp_path, monkeypatch):
    from cmp_api.main import sync_connection_profiles

    base = tmp_path / "base"
    base.write_text("[profile cmp-demo-build]\ncredential_source = Ec2InstanceMetadata\n")
    target = tmp_path / "shared/config"
    monkeypatch.setattr(settings, "connection_aws_config_file", str(target))
    monkeypatch.setattr(settings, "connection_aws_base_config_file", str(base))
    monkeypatch.setattr(settings, "connection_platform_aws_profile", "cmp-demo-build")
    with sessions() as session:
        add_connection(session)
    sync_connection_profiles(sessions)
    text = target.read_text()
    assert "[profile cmp-demo-build]" in text and f"[profile {PROFILE}]" in text
    assert "source_profile = cmp-demo-build" in text


def test_api_start_up_sync_is_a_no_op_without_the_setting(sessions, monkeypatch):
    from cmp_api.main import sync_connection_profiles

    monkeypatch.setattr(settings, "connection_aws_config_file", None)

    def never():
        raise AssertionError("no DB access when unconfigured")

    sync_connection_profiles(never)


def test_connection_responses_show_the_platform_principal(monkeypatch):
    from cmp_api.routes.connections import ConnectionDetail, CreateConnectionResponse, _detail

    monkeypatch.setattr(settings, "connection_platform_principal_arn", "arn:aws:iam::1:role/x")
    aws = EnvironmentConnection(
        id=CONN, kind="AWS_TARGET", display_name="t", region="ap-northeast-2",
        external_id="cmp-" + "a" * 32, status="PENDING", created_at=1,
    )  # fmt: skip
    gcp = EnvironmentConnection(
        id="conn-000000000000", kind="GCP_TARGET", display_name="g", region="asia-northeast3",
        status="PENDING", created_at=1,
    )  # fmt: skip
    assert _detail(aws)["platform_principal_arn"] == "arn:aws:iam::1:role/x"
    assert _detail(gcp)["platform_principal_arn"] is None
    assert "platform_principal_arn" in ConnectionDetail.model_fields
    assert "platform_principal_arn" in CreateConnectionResponse.model_fields


def test_ecr_token_minting_has_its_own_profile(monkeypatch):
    from cmp_api.routes import agents

    seen = []
    monkeypatch.setattr(agents, "boto3_minter", lambda profile: seen.append(profile))
    monkeypatch.setattr(settings, "connection_platform_aws_profile", "cmp-connections")
    monkeypatch.setattr(settings, "platform_ecr_aws_profile", "cmp-demo-build")
    agents.get_ecr_minter()
    monkeypatch.setattr(settings, "platform_ecr_aws_profile", None)
    agents.get_ecr_minter()
    assert seen == ["cmp-demo-build", "cmp-connections"]  # unset: ticket 41 behavior


def load(path: Path):
    return yaml.safe_load(path.read_text())


# --- demo wiring -----------------------------------------------------------------------------


def compose_services():
    return load(ROOT / "infra/demo/compose.yaml")["services"]


def test_api_writes_and_worker_reads_the_same_connection_config():
    services = compose_services()
    api, worker = services["api"], services["worker"]
    assert api["environment"]["CONNECTION_AWS_CONFIG_FILE"] == "/run/cmp/aws-connections/config"
    assert api["environment"]["CONNECTION_AWS_BASE_CONFIG_FILE"] == "/run/cmp/aws/config"
    assert worker["environment"]["AWS_CONFIG_FILE"] == "/run/cmp/aws-connections/config"
    assert "/opt/cmp/data/aws-connections:/run/cmp/aws-connections" in api["volumes"]
    assert "/opt/cmp/data/aws-connections:/run/cmp/aws-connections:ro" in worker["volumes"]
    env = api["environment"]
    # Ticket 23b: the connection principal stays the instance role (`cmp-demo-build`).
    assert env["CONNECTION_PLATFORM_AWS_PROFILE"] == "cmp-demo-build"
    assert env["CONNECTION_PLATFORM_PRINCIPAL_ARN"] == "${CONNECTION_PLATFORM_PRINCIPAL_ARN:-}"
    assert env["CONNECTION_TEMPLATE_BASE_URL"] == "${CONNECTION_TEMPLATE_BASE_URL:-}"
    assert env["PLATFORM_ECR_AWS_PROFILE"] == "cmp-demo-build"


def run_ensure_secrets(tmp_path, env_lines=""):
    env_file = tmp_path / ".env"
    env_file.write_text(env_lines)
    secrets = tmp_path / "secrets"
    script = ROOT / "infra/demo/ensure-secrets.sh"
    subprocess.run(
        ["bash", str(script), str(env_file), str(secrets), str(tmp_path / "aws-connections")],
        check=True,
        capture_output=True,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )
    return secrets


def test_ensure_secrets_seeds_the_shared_config_once(tmp_path):
    secrets = run_ensure_secrets(tmp_path)
    shared = tmp_path / "aws-connections/config"
    assert shared.read_text() == (secrets / "aws/config").read_text()
    assert oct(shared.stat().st_mode & 0o777) == "0o640"
    shared.write_text("generated by api\n")
    run_ensure_secrets(tmp_path)
    assert shared.read_text() == "generated by api\n"  # never clobbers the API's file


def test_generated_profiles_chain_to_the_instance_role_build_profile(tmp_path):
    """23b·46: `cmp-conn-*` → `cmp-demo-build` (credential_source, no role_arn) = instance role."""
    secrets = run_ensure_secrets(tmp_path, "PROVISIONING_AWS_ROLE_ARN=arn:aws:iam::1:role/other\n")
    base = secrets / "aws/config"
    connection = EnvironmentConnection(
        id=CONN, kind="AWS_TARGET", role_arn="arn:aws:iam::123456789012:role/cmp-t",
        external_id="cmp-" + "a" * 32, region="ap-northeast-2",
    )  # fmt: skip
    text = connections.render_aws_config(
        [connection], base=base.read_text(), platform_profile="cmp-demo-build"
    )
    parser = configparser.ConfigParser()
    parser.read_string(text)
    generated = parser[f"profile {PROFILE}"]
    assert generated["source_profile"] == "cmp-demo-build"
    platform = parser["profile cmp-demo-build"]
    assert platform["credential_source"] == "Ec2InstanceMetadata"
    assert "role_arn" not in platform  # never PROVISIONING_AWS_ROLE_ARN
    assert "aws_access_key_id" not in text
