"""Ticket 21 (#100): demo deployment can run Provisioning.

Static seams over `infra/demo`: compose environment and port exposure, worker image tool pins,
edge routes, host secret generation and the CloudFormation ingress. Real deployment, the public
certificate and the on-server smoke are verified on the demo host (see the ticket).
"""

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra" / "demo"


def _compose() -> dict:
    return yaml.safe_load((DEMO / "compose.yaml").read_text())


def _env(service: str) -> dict[str, str]:
    return _compose()["services"][service]["environment"]


def _dockerfile_args(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", path.read_text(), re.MULTILINE))


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def test_api_and_worker_share_database_and_artifact_store():
    api, worker = _env("api"), _env("worker")
    for name in (
        "DATABASE_URL",
        "TEMPORAL_ADDRESS",
        "ARTIFACT_STORE_DIR",
        "PROVISIONING_ARTIFACT_ROOT",
    ):
        assert api[name] == worker[name], name
    assert "${PLATFORM_DB_PASSWORD:?" in worker["DATABASE_URL"]
    services = _compose()["services"]
    api_mounts, worker_mounts = set(services["api"]["volumes"]), set(services["worker"]["volumes"])
    for path in (api["ARTIFACT_STORE_DIR"], api["PROVISIONING_ARTIFACT_ROOT"]):
        shared = {m for m in api_mounts & worker_mounts if m.split(":")[1] == path}
        assert len(shared) == 1, path
        assert shared.pop().startswith("/opt/cmp/data/")


def test_admin_keys_are_required_on_api_only():
    api, worker = _env("api"), _env("worker")
    for name in ("APPROVAL_ADMIN_KEY", "AGENT_ENROLLMENT_ADMIN_KEY"):
        assert api[name] == f"${{{name}:?set {name}}}"
        assert name not in worker


def test_only_edge_publishes_a_non_loopback_port_and_only_443():
    public = {}
    for name, service in _compose()["services"].items():
        for port in service.get("ports", []):
            if not str(port).startswith("127.0.0.1:"):
                public.setdefault(name, []).append(str(port))
    assert public == {"edge": ["443:443"]}
    edge = _compose()["services"]["edge"]
    assert edge["profiles"] == ["public"]
    assert "@sha256:" in edge["image"]


def test_cloud_credentials_are_mounted_read_only_not_set_as_values():
    services = _compose()["services"]
    worker = services["worker"]
    assert "/opt/cmp/secrets/aws:/run/cmp/aws:ro" in worker["volumes"]
    assert "/opt/cmp/secrets/gcp:/run/cmp/gcp:ro" in worker["volumes"]
    # Ticket 46: the worker reads the API-generated config (base profiles + `cmp-conn-*`).
    assert worker["environment"]["AWS_CONFIG_FILE"] == "/run/cmp/aws-connections/config"
    assert "/root/.config/gcloud:mode=0700" in worker["tmpfs"]
    forbidden = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CREDENTIALS",
    }
    for name, service in services.items():
        assert forbidden.isdisjoint(service.get("environment", {})), name
        for mount in service.get("volumes", []):
            if "/secrets/" in mount:
                assert mount.endswith(":ro"), mount


def test_worker_image_pins_every_cli_with_a_checksum():
    dockerfile = DEMO / "Dockerfile"
    args = _dockerfile_args(dockerfile)
    text = dockerfile.read_text()
    for tool in ("TERRAFORM", "KUBECTL", "HELM", "AWSCLI", "GCLOUD", "DOCKER", "BUILDX"):
        assert re.fullmatch(r"v?\d+\.\d+\.\d+", args[f"{tool}_VERSION"]), tool
        for arch in ("amd64", "arm64"):
            assert re.fullmatch(r"[0-9a-f]{64}", args[f"{tool}_SHA256_{arch}"]), (tool, arch)
    # Every download is verified before use.
    assert text.count("curl -fsSLo") == text.count("sha256sum -c -") == 7
    assert "components-v${GCLOUD_VERSION}.json" in text
    assert "gke-gcloud-auth-plugin" in text
    assert re.search(r"^FROM api AS worker$", text, re.MULTILINE)


def test_worker_kubectl_and_helm_match_the_agent_image():
    demo = _dockerfile_args(DEMO / "Dockerfile")
    agent = _dockerfile_args(ROOT / "infra" / "onprem-agent" / "Dockerfile")
    for name in agent:
        if name.startswith(("KUBECTL_", "HELM_")):
            assert demo[name] == agent[name], name


def test_terraform_pin_satisfies_template_constraints():
    major, minor, _ = (
        int(p) for p in _dockerfile_args(DEMO / "Dockerfile")["TERRAFORM_VERSION"].split(".")
    )
    for target in ("aws", "gcp"):
        versions = (ROOT / "infra" / "terraform" / target / "versions.tf").read_text()
        assert 'required_version = ">= 1.10.0, < 2.0.0"' in versions
    assert major == 1 and minor >= 10


def test_compose_targets_slim_api_and_tool_worker():
    services = _compose()["services"]
    assert services["api"]["build"]["target"] == "api"
    assert services["worker"]["build"]["target"] == "worker"


def test_edge_sends_only_agent_and_relay_routes_to_the_api():
    routes = (DEMO / "agent-routes.caddy").read_text()
    code = [
        line.strip()
        for line in routes.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Everything that is not the Agent API or the relay polling API is the web front end; the
    # operator, approval and tunnel-admin routes and /docs never get a route to the API.
    assert code == [
        "@temporal {",
        "path /temporal.api.* /grpc.health.v1.Health/*",
        "remote_ip {$CMP_TEMPORAL_ALLOWED_IP}",
        "}",
        "handle @temporal {",
        "reverse_proxy h2c://{$CMP_TEMPORAL_UPSTREAM}",
        "}",
        "@backend path /agents/* /relays/*",
        "handle @backend {",
        "reverse_proxy {$CMP_API_UPSTREAM}",
        "}",
        "handle {",
        "reverse_proxy {$CMP_WEB_UPSTREAM}",
        "}",
    ]
    caddyfile = (DEMO / "Caddyfile").read_text()
    assert "import agent-routes.caddy" in caddyfile
    assert "disable_http_challenge" in caddyfile
    assert "auto_https disable_redirects" in caddyfile
    edge = _env("edge")
    assert edge["CMP_API_UPSTREAM"] == "api:8000"
    assert edge["CMP_WEB_UPSTREAM"] == "web:3000"
    assert edge["CMP_TEMPORAL_UPSTREAM"] == "temporal:7233"
    # Nobody may reach Temporal through the edge unless the operator sets an address.
    assert edge["CMP_TEMPORAL_ALLOWED_IP"].endswith("192.0.2.1/32}")


def test_web_is_internal_only_and_reaches_the_api_over_the_compose_network():
    web = _compose()["services"]["web"]
    assert web["profiles"] == ["public"]
    assert "ports" not in web
    assert web["expose"] == ["3000"]
    env = web["environment"]
    assert env["PLATFORM_API_BASE"] == "http://api:8000"
    assert env["AUTH_TRUST_HOST"] == "true"
    # Secrets come from /opt/cmp/.env (generated once); the web never gets DB, approval or cloud credentials.
    assert env["AUTH_SECRET"] == "${WEB_AUTH_SECRET:-}"
    # The web's /api/agents routes mint and revoke Agent enrollments with the admin key but have no user
    # login yet, so the public web must not get that key by default (an operator may add it to
    # /opt/cmp/web.env once those routes are authenticated).
    for name in (
        "PLATFORM_ENROLLMENT_ADMIN_KEY",
        "AGENT_ENROLLMENT_ADMIN_KEY",
        "DATABASE_URL",
        "APPROVAL_ADMIN_KEY",
        "AWS_CONFIG_FILE",
        "CMP_GCP_KEY_FILE",
    ):
        assert name not in env, name
    assert not any("/opt/cmp/secrets" in mount for mount in web.get("volumes", []))
    # Built on the host from the web release that release-web.sh extracts; a missing web release
    # must not break the API deploy.
    assert web["build"]["context"] == "${CMP_WEB_DIR:-/opt/cmp/web/current}"
    assert {"path": "/opt/cmp/web.env", "required": False} in web["env_file"]


def test_release_web_script_uploads_only_under_releases_and_checks_the_account():
    script = (DEMO / "release-web.sh").read_text()
    assert "714066696240" in script
    assert "releases/web-" in script
    assert "ln -sfn" in script and "/opt/cmp/web/current" in script
    syntax = subprocess.run(["bash", "-n", str(DEMO / "release-web.sh")], check=False)
    assert syntax.returncode == 0


def test_security_group_opens_only_https_and_imds_reaches_containers():
    template = yaml.load((DEMO / "cloudformation.yaml").read_text(), Loader=_CfnLoader)
    resources = template["Resources"]
    ingress = resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert [(r["IpProtocol"], r["FromPort"], r["ToPort"]) for r in ingress] == [("tcp", 443, 443)]
    metadata = resources["Instance"]["Properties"]["MetadataOptions"]
    assert metadata["HttpTokens"] == "required"
    assert metadata["HttpPutResponseHopLimit"] == 2
    assert resources["PublicAddress"]["Type"] == "AWS::EC2::EIP"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_ensure_secrets_generates_missing_keys_and_keeps_existing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PLATFORM_DB_PASSWORD=keep-me\nPROVISIONING_AWS_ROLE_ARN=arn:aws:iam::1:role/x\n"
    )
    secrets = tmp_path / "secrets"
    script = DEMO / "ensure-secrets.sh"

    subprocess.run(["bash", str(script), str(env_file), str(secrets)], check=True)
    first = env_file.read_text()
    subprocess.run(["bash", str(script), str(env_file), str(secrets)], check=True)

    assert env_file.read_text() == first
    values = dict(line.split("=", 1) for line in first.splitlines())
    assert values["PLATFORM_DB_PASSWORD"] == "keep-me"
    for name in (
        "TEMPORAL_DB_PASSWORD",
        "AGENT_ENROLLMENT_ADMIN_KEY",
        "APPROVAL_ADMIN_KEY",
        "WEB_AUTH_SECRET",
    ):
        assert re.fullmatch(r"[0-9a-f]{48,64}", values[name]), name
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    config = (secrets / "aws" / "config").read_text()
    assert "[profile cmp-demo-provisioning]" in config
    assert "credential_source = Ec2InstanceMetadata" in config
    assert "role_arn = arn:aws:iam::1:role/x" in config
    assert "aws_access_key_id" not in config and "aws_secret_access_key" not in config
    assert stat.S_IMODE((secrets / "aws" / "config").stat().st_mode) == 0o600
    assert stat.S_IMODE((secrets / "gcp").stat().st_mode) == 0o700
    assert not (secrets / "gcp" / "key.json").exists()


def test_start_script_uses_secret_helper_and_checks_worker_tools():
    start = (DEMO / "start.sh").read_text()
    assert "ensure-secrets.sh /opt/cmp/.env /opt/cmp/secrets" in start
    assert "openssl rand" not in start
    assert "--profile public up -d edge" in start
    # The web front end is built from the extracted web release when there is one.
    assert "/opt/cmp/web/current/Dockerfile" in start
    assert "--profile public up -d --build web" in start
    assert start.index("up -d --build web") < start.index("up -d edge")
    for tool in (
        "terraform version",
        "helm version",
        "kubectl version",
        "aws --version",
        "gcloud --version",
    ):
        assert tool in start


def test_worker_entrypoint_activates_gcp_key_into_config_dir_without_printing_it(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    fake = bin_dir / "gcloud"
    fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{calls}"\n')
    fake.chmod(0o755)
    key = tmp_path / "key.json"
    key.write_text(
        '{"type": "service_account", "client_email": "cmp@p.iam.gserviceaccount.com", "private_key": "SECRET"}'
    )
    config_dir = tmp_path / "gcloud"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CLOUDSDK_CONFIG": str(config_dir),
        "CMP_GCP_KEY_FILE": str(key),
        "CMP_GCP_PROJECT": "cmp-demo-project",
        "PROVISIONING_GCLOUD_CONFIGURATION": "cmp-demo",
    }
    python = shutil.which("python3")
    (bin_dir / "python").symlink_to(python)

    result = subprocess.run(
        [
            "bash",
            str(DEMO / "worker-entrypoint.sh"),
            "sh",
            "-c",
            'echo "adc=$PROVISIONING_GCP_ADC_EMAIL"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "adc=cmp@p.iam.gserviceaccount.com" in result.stdout
    assert "SECRET" not in result.stdout + result.stderr
    adc = config_dir / "application_default_credentials.json"
    assert adc.read_text() == key.read_text()
    assert stat.S_IMODE(adc.stat().st_mode) == 0o600
    logged = calls.read_text()
    assert "auth activate-service-account cmp@p.iam.gserviceaccount.com --key-file" in logged
    assert "config set project cmp-demo-project --configuration cmp-demo" in logged


def test_worker_entrypoint_without_key_runs_command_and_fails_closed_later(tmp_path):
    result = subprocess.run(
        ["bash", str(DEMO / "worker-entrypoint.sh"), "true"],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PROVISIONING_GCLOUD_CONFIGURATION": "cmp-demo",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "GCP identity not configured" in result.stderr


# Ticket 41 (#176): the API mints K3s Agent ECR pull tokens and names the platform registry in
# ON_PREM rollout jobs, so it needs the instance-role build profile and the registry settings.


def test_api_uses_the_instance_role_build_profile_without_gcp_credentials():
    api = _compose()["services"]["api"]
    env = api["environment"]
    assert env["AWS_CONFIG_FILE"] == "/run/cmp/aws/config"
    assert "/opt/cmp/secrets/aws:/run/cmp/aws:ro" in api["volumes"]
    # Same profile the worker's Build module uses; ensure-secrets.sh writes it as the instance
    # role itself (never PROVISIONING_AWS_ROLE_ARN).
    # Ticket 23b·46: the connection principal stays this profile; ECR tokens have their own.
    assert env["CONNECTION_PLATFORM_AWS_PROFILE"] == "cmp-demo-build"
    # Ticket 47 (B-09): the pull-only profile once configured, the build profile until then.
    assert env["PLATFORM_ECR_AWS_PROFILE"] == "${PLATFORM_ECR_AWS_PROFILE:-cmp-demo-build}"
    assert "PROVISIONING_AWS_PROFILE" not in env
    # The API never gets the GCP key or a gcloud configuration.
    assert not [m for m in api["volumes"] if "/secrets/gcp" in m or "/run/cmp/gcp" in m]
    assert not [name for name in env if "GCP" in name or "GCLOUD" in name]


def test_api_platform_registry_settings_default_to_disabled():
    env = _env("api")
    assert env["PLATFORM_ECR_REGISTRY"] == "${PLATFORM_ECR_REGISTRY:-}"
    assert env["PLATFORM_ECR_REPOSITORIES"] == "${PLATFORM_ECR_REPOSITORIES:-cmp-platform-builds}"
    from cmp_providers.ecr_pull import PlatformRegistry

    # An empty registry (unset in /opt/cmp/.env) keeps today's behavior: no platform registry.
    assert PlatformRegistry.parse("", "cmp-platform-builds") is None


def _run_deploy_build_stack(tmp_path, uri: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "git": "exit 0\n",  # `git status --porcelain`: clean tree
        "aws": f"""case "$*" in
  *get-caller-identity*) echo 714066696240 ;;
  *describe-stack-resource*) echo cmp-demo-control-plane-InstanceRole-ABC ;;
  *DeployBucket*) echo cmp-demo-714066696240-apne2-deploy ;;
  *RepositoryUri*) echo {uri} ;;
  *PullRoleArn*) echo arn:aws:iam::714066696240:role/cmp-demo-ecr-pull ;;
  *"cloudformation deploy"*) ;;
  *) echo "unexpected aws $*" >&2; exit 9 ;;
esac
""",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
    return subprocess.run(
        ["bash", str(DEMO / "deploy-build-stack.sh")],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_stack_apply_prints_the_platform_registry_from_the_repository_uri(tmp_path):
    registry = "714066696240.dkr.ecr.ap-northeast-2.amazonaws.com"
    result = _run_deploy_build_stack(tmp_path, f"{registry}/cmp-platform-builds")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert f"PLATFORM_ECR_REGISTRY={registry}" in lines
    assert f"BUILD_ECR_REPOSITORY={registry}/cmp-platform-builds" in lines


def test_build_stack_apply_refuses_a_repository_outside_the_expected_registry(tmp_path):
    result = _run_deploy_build_stack(
        tmp_path, "111111111111.dkr.ecr.us-east-1.amazonaws.com/cmp-platform-builds"
    )
    assert result.returncode != 0
    assert "PLATFORM_ECR_REGISTRY=" not in result.stdout


# -- Ticket 45 (#180): analysis·Sandbox·ENV·catalog wiring and the read-only server check ------


def _runtime_example() -> dict[str, str]:
    """`infra/demo/runtime.env.example`: the optional `/opt/cmp/.env` lines (no secrets)."""
    lines = (DEMO / "runtime.env.example").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def _mount_targets(service: str) -> dict[str, str]:
    """container path -> host path."""
    mounts = _compose()["services"][service]["volumes"]
    return {m.split(":")[1]: m.split(":")[0] for m in mounts}


def test_env_file_store_is_shared_by_api_and_worker_with_a_generated_key():
    api, worker = _env("api"), _env("worker")
    for env in (api, worker):
        assert env["ENV_FILE_STORE_DIR"] == "/opt/cmp/env-files"
        assert env["ENV_FILE_ENCRYPTION_KEY"] == (
            "${ENV_FILE_ENCRYPTION_KEY:?set ENV_FILE_ENCRYPTION_KEY}"
        )
    services = _compose()["services"]
    # The API writes uploads; the worker reads names/values (EnvPlan, render_env). Not `:ro`:
    # opening the store chmods its root (local compose run: the worker crashed with EROFS).
    for service in ("api", "worker"):
        assert "/opt/cmp/data/env-files:/opt/cmp/env-files" in services[service]["volumes"]
    assert "ENV_FILE_ENCRYPTION_KEY" not in services["web"].get("environment", {})


def test_ensure_secrets_generates_an_env_file_key_the_store_accepts_once(tmp_path):
    from cmp_providers.env_file_store import load_key

    env_file = tmp_path / ".env"
    secrets = tmp_path / "secrets"
    script = DEMO / "ensure-secrets.sh"
    subprocess.run(["bash", str(script), str(env_file), str(secrets)], check=True)
    first = env_file.read_text()
    subprocess.run(["bash", str(script), str(env_file), str(secrets)], check=True)
    assert env_file.read_text() == first  # never regenerated: old uploads stay readable
    values = dict(line.split("=", 1) for line in first.splitlines())
    assert len(load_key(values["ENV_FILE_ENCRYPTION_KEY"])) == 32
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", values["ENV_FILE_ENCRYPTION_KEY"])
    # The Sandbox kubeconfig directory exists (empty) so its read-only mount never fails.
    assert stat.S_IMODE((secrets / "sandbox").stat().st_mode) == 0o700
    assert not list((secrets / "sandbox").iterdir())


def test_worker_analysis_sandbox_and_llm_settings_default_to_the_scaffold():
    worker = _env("worker")
    expected = {
        "ANALYSIS_ARTIFACT_STORE_DIR": "${ANALYSIS_ARTIFACT_STORE_DIR:-}",
        "ANALYSIS_WORKSPACE_ROOT": "${ANALYSIS_WORKSPACE_ROOT:-}",
        "SANDBOX_KUBECONFIG": "${SANDBOX_KUBECONFIG:-}",
        "SANDBOX_PREBUILT_IMAGES": "${SANDBOX_PREBUILT_IMAGES:-}",
        "LLM_ENABLED": "${LLM_ENABLED:-false}",
        "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY:-}",
        "LLM_LEDGER_PATH": "/opt/cmp/llm/ledger.json",
        "LLM_KILL_SWITCH_PATH": "/opt/cmp/llm/disabled",
    }
    assert {name: worker.get(name) for name in expected} == expected
    from cmp_worker.config import Settings
    from cmp_worker.llm_factory import build_llm_factory
    from cmp_worker.workers.analysis import build_pipeline
    from cmp_worker.workers.sandbox import build_sandbox_loop

    # Unset in /opt/cmp/.env (compose passes empty strings): today's scaffold registrations.
    empty = Settings(
        _env_file=None,
        analysis_artifact_store_dir="",
        analysis_workspace_root="",
        sandbox_kubeconfig="",
        sandbox_prebuilt_images="",
        llm_enabled=False,
        llm_ledger_path=expected["LLM_LEDGER_PATH"],
    )
    assert build_pipeline(empty) is None
    assert build_sandbox_loop(empty) is None
    assert build_llm_factory(empty) is None
    for name in ("ANTHROPIC_API_KEY", "LLM_ENABLED", "SANDBOX_KUBECONFIG"):
        assert name not in _env("api"), name


def test_runtime_example_values_point_at_the_mounted_worker_paths():
    example, worker = _runtime_example(), _env("worker")
    targets = _mount_targets("worker")
    for name in example:
        assert f"${{{name}:-" in str(worker.get(name)), name  # every example line is consumed
    # Planning reads the analysis results from the same store (runbook: same directory).
    assert example["ANALYSIS_ARTIFACT_STORE_DIR"] == worker["ARTIFACT_STORE_DIR"]
    assert targets["/opt/cmp/analysis-workspaces"] == "/opt/cmp/data/analysis-workspaces"
    assert example["ANALYSIS_WORKSPACE_ROOT"] == "/opt/cmp/analysis-workspaces"
    assert targets["/run/cmp/sandbox"] == "/opt/cmp/secrets/sandbox"
    assert targets["/opt/cmp/llm"] == "/opt/cmp/data/llm"
    # Values owned by analysis/Sandbox (박승철님) and secrets stay empty in the repository.
    for name in ("SANDBOX_KUBECONFIG", "SANDBOX_PREBUILT_IMAGES", "ANTHROPIC_API_KEY"):
        assert example[name] == "", name
    assert example["LLM_ENABLED"] == "false"


def test_start_script_creates_the_runtime_data_directories():
    start = (DEMO / "start.sh").read_text()
    for path in (
        "/opt/cmp/data/env-files",
        "/opt/cmp/data/analysis-workspaces",
        "/opt/cmp/data/llm",
    ):
        assert path in start, path
    for service in ("api", "worker"):
        for host in _mount_targets(service).values():
            if host.startswith("/opt/cmp/data/") and host != "/opt/cmp/data/aws-connections":
                assert host in start, host


def test_worker_runtime_report_names_real_or_scaffold_paths_only(tmp_path, monkeypatch):
    from cmp_worker.config import Settings
    from cmp_worker.runtime_report import report

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    scaffold = report(Settings(_env_file=None))
    assert scaffold["analysis"] == "scaffold"
    assert scaffold["sandbox"] == "scaffold"
    assert scaffold["build"] == "off"
    assert scaffold["llm"] == "off"

    real = report(
        Settings(
            _env_file=None,
            analysis_artifact_store_dir=str(tmp_path / "store"),
            analysis_workspace_root=str(tmp_path / "work"),
            sandbox_kubeconfig=str(tmp_path / "missing-kubeconfig"),
            build_source_bucket="only-one-of-four",
        )
    )
    assert real["analysis"] == "real"
    assert real["sandbox"] == "error:SANDBOX_KUBECONFIG_NOT_FOUND"
    assert real["build"] == "error:BUILD_SETTINGS_INCOMPLETE"


def test_api_runtime_report_names_store_catalog_and_link_state(tmp_path, monkeypatch):
    import base64

    from cmp_api.config import settings
    from cmp_api.runtime_report import report

    for name in (
        "env_file_store_dir",
        "env_file_encryption_key",
        "planning_target_profiles_file",
        "connection_platform_principal_arn",
        "connection_template_base_url",
        "platform_ecr_registry",
    ):
        monkeypatch.setattr(settings, name, None)
    off = report()
    assert off["env_store"] == "error:ENV_FILE_STORE_UNCONFIGURED"
    assert off["target_profiles"] == "error:TARGET_PROFILES_UNCONFIGURED"
    assert off["connection_link"] == "off"
    assert off["platform_ecr_registry"] == "off"

    from pydantic import SecretStr

    catalog = tmp_path / "target-profiles.json"
    catalog.write_text((DEMO / "target-profiles.example.json").read_text())
    monkeypatch.setattr(settings, "env_file_store_dir", str(tmp_path / "env-files"))
    monkeypatch.setattr(
        settings,
        "env_file_encryption_key",
        SecretStr(base64.urlsafe_b64encode(b"k" * 32).decode()),
    )
    monkeypatch.setattr(settings, "planning_target_profiles_file", str(catalog))
    on = report()
    assert on["env_store"] == "on"
    assert on["target_profiles"] == "aws-eks-basic,gcp-vm-basic,onprem-k3s-basic"


def _run_runtime_check(tmp_path, env_text: str) -> subprocess.CompletedProcess:
    root = tmp_path / "opt-cmp"
    root.mkdir()
    (root / ".env").write_text(env_text)
    (root / "current-revision").write_text("0123456789ab\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "docker": """case "$*" in
  *"alembic current"*) echo "20261106_02 (head)" ;;
  *cmp_worker.runtime_report*) printf 'analysis=scaffold\\nsandbox=scaffold\\n' ;;
  *cmp_api.runtime_report*) printf 'env_store=on\\ntarget_profiles=onprem-k3s-basic\\n' ;;
  *"ps "*) echo "cmp-demo-worker-1 running" ;;
  *) echo "unexpected docker $*" >&2; exit 9 ;;
esac
""",
        "iptables": "echo '-A DOCKER-USER -i docker0 -m comment --comment cmp-build-imds-guard'\n",
        "systemctl": "echo enabled\n",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
    return subprocess.run(
        ["bash", str(DEMO / "runtime-check.sh"), str(root)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_runtime_check_prints_names_and_states_never_env_values(tmp_path):
    secret = "do-not-print-0123456789abcdef"
    result = _run_runtime_check(
        tmp_path,
        f"APPROVAL_ADMIN_KEY={secret}\nENV_FILE_ENCRYPTION_KEY={secret}x\n"
        f"ANTHROPIC_API_KEY={secret}y\nBUILD_PLATFORM=linux/amd64\n",
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert secret not in out and "linux/amd64" not in out
    for expected in (
        "revision=0123456789ab",
        "alembic=20261106_02 (head)",
        "analysis=scaffold",
        "env_store=on",
        "target_profiles=onprem-k3s-basic",
        "imds_guard_rules=1",
        "imds_guard_service=enabled",
        "APPROVAL_ADMIN_KEY",
        "BUILD_PLATFORM",
    ):
        assert expected in out, expected
    # Names the demo needs but .env lacks are listed as missing (here: the DB passwords).
    assert "missing=" in out and "PLATFORM_DB_PASSWORD" in out.split("missing=", 1)[1]
    optional = out.split("optional_unset=", 1)[1].splitlines()[0].split()
    for name in (
        "PLATFORM_ECR_PULL_ROLE_ARN",
        "PROVISIONING_PLATFORM_EGRESS_CIDRS",
        "CMP_GCP_PROJECT",
    ):
        assert name in optional, name  # 47·48 names are reported when unset
    assert "BUILD_PLATFORM" not in optional


def test_worker_image_keeps_git_for_the_real_analysis_source_fetch():
    """Ticket 45 local run: real analysis `fetch_source` failed ANALYSIS_INPUT_INVALID because
    `git` was missing (FileNotFoundError). The release health check now requires it."""
    dockerfile = (DEMO / "Dockerfile").read_text()
    worker_stage = dockerfile.split("FROM api AS worker", 1)[1]
    install = re.search(r"apt-get install -y --no-install-recommends ([^;]+);", worker_stage)
    assert install and "git" in install.group(1).split()
    purge = re.search(r"apt-get purge -y ([^;]+);", worker_stage)
    assert purge and "git" not in purge.group(1).split()
    assert "git --version" in (DEMO / "start.sh").read_text()
