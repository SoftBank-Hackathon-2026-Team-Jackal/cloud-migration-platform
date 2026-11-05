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
    assert worker["environment"]["AWS_CONFIG_FILE"] == "/run/cmp/aws/config"
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


def test_edge_forwards_only_agent_routes():
    routes = (DEMO / "agent-routes.caddy").read_text()
    code = [
        line.strip()
        for line in routes.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert code == [
        "@agent path /agents/*",
        "handle @agent {",
        "reverse_proxy {$CMP_API_UPSTREAM}",
        "}",
        "handle {",
        "respond 404",
        "}",
    ]
    caddyfile = (DEMO / "Caddyfile").read_text()
    assert "import agent-routes.caddy" in caddyfile
    assert "disable_http_challenge" in caddyfile
    assert "auto_https disable_redirects" in caddyfile


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
    for name in ("TEMPORAL_DB_PASSWORD", "AGENT_ENROLLMENT_ADMIN_KEY", "APPROVAL_ADMIN_KEY"):
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
