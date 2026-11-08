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
    assert re.search(r"^FROM base AS worker$", text, re.MULTILINE)


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


def test_edge_sends_agent_relay_and_migration_routes_to_the_api():
    routes = (DEMO / "agent-routes.caddy").read_text()
    code = [
        line.strip()
        for line in routes.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # #225: the browser (same origin) starts and follows migrations at /migrations and approves
    # Plans at /provisioning/*; the SSE stream is passed through unbuffered. Operator, Agent
    # enrollment and tunnel-admin routes (/agent-admin/*, /agent-enrollments, /tunnel-admin/*,
    # /provisioning-admin/*, /connections, /env-files) and /docs still never reach the API.
    assert code == [
        "@temporal {",
        "path /temporal.api.* /grpc.health.v1.Health/*",
        "remote_ip {$CMP_TEMPORAL_ALLOWED_IP}",
        "}",
        "handle @temporal {",
        "reverse_proxy h2c://{$CMP_TEMPORAL_UPSTREAM}",
        "}",
        "@migration_events path /migrations/*/events",
        "handle @migration_events {",
        "reverse_proxy {$CMP_API_UPSTREAM} {",
        "flush_interval -1",
        "lb_try_duration 15s",
        "lb_try_interval 250ms",
        "}",
        "}",
        "@pulse_report {",
        "method POST",
        "path /system/pulse/report",
        "}",
        "handle @pulse_report {",
        "reverse_proxy {$CMP_API_UPSTREAM} {",
        "lb_try_duration 15s",
        "lb_try_interval 250ms",
        "}",
        "}",
        "@traffic_health_report {",
        "method POST",
        "path /system/traffic-health/report /system/traffic-health/fault",
        "}",
        "handle @traffic_health_report {",
        "reverse_proxy {$CMP_API_UPSTREAM} {",
        "lb_try_duration 15s",
        "lb_try_interval 250ms",
        "}",
        "}",
        "@otlp_metrics {",
        "method POST",
        "path /otlp/v1/metrics",
        "header X-Otlp-Token {$CMP_OTLP_TOKEN}",
        "}",
        "handle @otlp_metrics {",
        "rewrite * /api/v1/otlp/v1/metrics",
        "reverse_proxy {$CMP_PROMETHEUS_UPSTREAM}",
        "}",
        "@otlp_traces {",
        "method POST",
        "path /otlp/v1/traces",
        "header X-Otlp-Token {$CMP_OTLP_TOKEN}",
        "}",
        "handle @otlp_traces {",
        "rewrite * /v1/traces",
        "reverse_proxy {$CMP_JAEGER_UPSTREAM}",
        "}",
        "handle /otlp/* {",
        "respond 401",
        "}",
        "@backend path /agents/* /relays/* /migrations /migrations/* /provisioning/*",
        "handle @backend {",
        "reverse_proxy {$CMP_API_UPSTREAM} {",
        "lb_try_duration 15s",
        "lb_try_interval 250ms",
        "}",
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


def test_web_is_built_in_real_sse_mode_against_the_public_origin():
    # NEXT_PUBLIC_* values are compiled into the browser bundle, so they are build args, not
    # runtime environment. cloud-migration-web's Dockerfile declares these ARGs (default
    # mock/wait/empty). The browser calls the same origin, which the edge sends to the API.
    web = _compose()["services"]["web"]
    assert web["build"]["args"] == {
        "NEXT_PUBLIC_API_MODE": "sse",
        "NEXT_PUBLIC_SSE_START": "source",
        "NEXT_PUBLIC_API_BASE": "https://${CMP_PUBLIC_HOST:-localhost}",
        # Ticket 77: the web's real-mode screens backed by platform APIs that exist (report #240,
        # traffic rollback/retry #245). sandbox.retry has no platform API and stays off.
        "NEXT_PUBLIC_SSE_CAPABILITIES": "${WEB_SSE_CAPABILITIES:-report,traffic.rollback,traffic.retry,traffic.finalize}",
        # Temporal sidebar: next.config.ts rewrites are fixed at build time, /api/temporal reads it at run time.
        "TEMPORAL_UI_URL": "http://temporal-ui:8080",
    }
    for name in web["build"]["args"]:
        if name.startswith("NEXT_PUBLIC_"):
            assert name not in web["environment"], name
    assert web["environment"]["TEMPORAL_UI_URL"] == web["build"]["args"]["TEMPORAL_UI_URL"]
    # An sse build would otherwise switch the web's connection screens to the real API, which
    # needs the operator key the public web does not hold; keep today's mock default.
    assert web["environment"]["PLATFORM_CONNECTIONS"] == "${WEB_PLATFORM_CONNECTIONS:-mock}"


def test_app_telemetry_ingest_needs_the_generated_token_and_stays_off_the_migration_path():
    services = _compose()["services"]
    edge = _env("edge")
    # The edge refuses to start without a token (ensure-secrets.sh generates it), so /otlp/* is never
    # open to anyone without the header.
    assert edge["CMP_OTLP_TOKEN"] == "${OTLP_INGEST_TOKEN:?set OTLP_INGEST_TOKEN}"
    assert "ensure_key OTLP_INGEST_TOKEN 32" in (DEMO / "ensure-secrets.sh").read_text()
    assert edge["CMP_PROMETHEUS_UPSTREAM"] == "prometheus:9090"
    assert edge["CMP_JAEGER_UPSTREAM"] == "jaeger:4318"
    prometheus = services["prometheus"]
    assert "--web.enable-otlp-receiver" in prometheus["command"]
    assert prometheus["ports"] == ["127.0.0.1:9090:9090"]
    assert services["jaeger"]["ports"] == ["127.0.0.1:16686:16686"]
    config = yaml.safe_load((DEMO / "prometheus.yml").read_text())
    assert "app.env" in config["otlp"]["promote_resource_attributes"]
    # Nothing on the migration path depends on them.
    for name in ("api", "worker", "temporal", "edge"):
        assert not {"prometheus", "jaeger"} & set(services[name].get("depends_on") or [])
    assert _env("web")["PROMETHEUS_URL"] == "http://prometheus:9090"
    assert _env("worker")["CMP_TELEMETRY_EGRESS_CIDR"] == "${CMP_TELEMETRY_EGRESS_CIDR:-}"
    for script in ("start.sh", "start-image.sh"):
        assert "temporal-ui prometheus jaeger" in (DEMO / script).read_text()


def test_temporal_ui_proxied_by_the_public_web_is_read_only():
    # The web sidebar exposes the Temporal UI on the public host without a login, so the UI must not
    # offer cancel/terminate/signal/reset. Operators use the temporal CLI instead (reset-rehearsal.sh).
    ui = _compose()["services"]["temporal-ui"]
    assert ui["environment"]["TEMPORAL_DISABLE_WRITE_ACTIONS"] == "true"
    assert ui["ports"] == ["127.0.0.1:8080:8080"]


def test_release_web_script_uploads_only_under_releases_and_checks_the_account():
    script = (DEMO / "release-web.sh").read_text()
    assert "714066696240" in script
    assert "releases/web-" in script
    assert "ln -sfn" in script and "/opt/cmp/web/current" in script
    # The web is rebuilt with the active release's compose file, so its build args apply.
    assert "-f infra/demo/compose.yaml --profile public up -d --build --no-deps web" in script
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


def test_worker_gets_the_same_platform_registry_settings_as_the_api():
    # The worker builds ON_PREM rollout jobs (execute_on_prem_plan → build_rollout_job), which
    # read cmp_api settings; without these an ECR app image is IMAGE_REGISTRY_NOT_ALLOWED.
    api, worker = _env("api"), _env("worker")
    for name in ("PLATFORM_ECR_REGISTRY", "PLATFORM_ECR_REPOSITORIES"):
        assert name in worker, name
        assert worker[name] == api[name], name


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
    # The API holds the key only for the agent chat (`/agent/ask`, internal-only, ledger-limited,
    # kill-switch aware). It still gets no analysis/sandbox/LLM-pipeline settings.
    for name in ("LLM_ENABLED", "SANDBOX_KUBECONFIG"):
        assert name not in _env("api"), name
    assert _env("api")["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"


def test_runtime_example_values_point_at_the_mounted_worker_paths():
    example, worker, api = _runtime_example(), _env("worker"), _env("api")
    targets = _mount_targets("worker")
    for name in example:
        # ticket 57, data-migration ticket 08 and the agent chat: API only
        consumer = api if name.startswith(("WEB_START_", "AGENT_CHAT_", "PULSE_")) else worker
        assert f"${{{name}:-" in str(consumer.get(name)), name  # every example line is consumed
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


def test_worker_runtime_report_names_the_rollout_platform_registry_state(monkeypatch):
    from cmp_api.config import settings as api_settings
    from cmp_worker.config import Settings
    from cmp_worker.runtime_report import report

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    host = "714066696240.dkr.ecr.ap-northeast-2.amazonaws.com"
    monkeypatch.setattr(api_settings, "platform_ecr_registry", None)
    assert report(Settings(_env_file=None))["platform_ecr_registry"] == "off"
    monkeypatch.setattr(api_settings, "platform_ecr_registry", host)
    on = report(Settings(_env_file=None))["platform_ecr_registry"]
    assert on == "on"
    assert host not in "".join(report(Settings(_env_file=None)).values())
    monkeypatch.setattr(api_settings, "platform_ecr_registry", "not a registry")
    bad = report(Settings(_env_file=None))["platform_ecr_registry"]
    assert bad == "error:PLATFORM_REGISTRY_INVALID"


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
    worker_stage = dockerfile.split("FROM base AS worker", 1)[1]
    install = re.search(r"apt-get install -y --no-install-recommends ([^;]+);", worker_stage)
    assert install and "git" in install.group(1).split()
    purge = re.search(r"apt-get purge -y ([^;]+);", worker_stage)
    assert purge and "git" not in purge.group(1).split()
    assert "git --version" in (DEMO / "start.sh").read_text()


# --- ticket 57 (#228): the rehearsal's default write/read shape for web starts ----------------


def test_web_start_sandbox_settings_are_an_api_value_off_by_default():
    # Server environment only (no app path in code); empty keeps the health-only rehearsal.
    assert _env("api")["WEB_START_SANDBOX_SETTINGS"] == "${WEB_START_SANDBOX_SETTINGS:-}"
    assert "WEB_START_SANDBOX_SETTINGS" not in _env("worker")


def test_runtime_example_sandbox_settings_match_the_demo_app_write_api():
    import json

    from cmp_testsuite.standard import AppApi, standard_cases
    from cmp_worker.sandbox_input import SandboxSettings

    raw = _runtime_example()["WEB_START_SANDBOX_SETTINGS"]
    # Single quotes: docker compose reads the JSON literally (no `$` interpolation).
    assert raw.startswith("'") and raw.endswith("'")
    value = json.loads(raw[1:-1])
    settings = SandboxSettings.model_validate(value)  # the worker accepts it as is
    # demo-app main.go: POST /api/messages {client_msg_id, author, body} answers 201 with the row
    # (server-made `id`), the same client_msg_id again answers 200 with the stored row; there is
    # no GET by id, so the read-back is the idempotent re-send.
    assert settings.write_path == "/api/messages"
    assert settings.write_status == 201
    assert settings.id_from_response == "id"
    assert settings.value_field == "body"
    assert settings.read_back == "REPEAT_WRITE"
    assert set(settings.write_body or {}) == {"client_msg_id", "author", "body"}
    api = AppApi(health_path="/healthz", **value)
    assert [case.id for case in standard_cases(api)] == [
        "std-health",
        "std-db-roundtrip",
        "std-survives-db-restart",
    ]


def test_api_runtime_report_names_the_web_start_sandbox_state(monkeypatch):
    from cmp_api.config import settings
    from cmp_api.runtime_report import report

    monkeypatch.setattr(settings, "web_start_sandbox_settings", None)
    assert report()["web_start_sandbox"] == "off"
    monkeypatch.setattr(settings, "web_start_sandbox_settings", '{"write_path": "/api/x"}')
    assert report()["web_start_sandbox"] == "on"
    monkeypatch.setattr(settings, "web_start_sandbox_settings", "[1]")
    assert report()["web_start_sandbox"] == "error:SANDBOX_SETTINGS_INVALID"


# --- data-migration ticket 08: the data step's input for web starts ----------------------------


def test_web_start_data_migration_is_an_api_value_off_by_default():
    assert _env("api")["WEB_START_DATA_MIGRATION"] == "${WEB_START_DATA_MIGRATION:-}"
    assert "WEB_START_DATA_MIGRATION" not in _env("worker")
    assert _runtime_example()["WEB_START_DATA_MIGRATION"] == ""  # depends on the WireGuard path
    assert "WEB_START_DATA_MIGRATION" in (DEMO / "runtime-check.sh").read_text()


def test_api_runtime_report_names_the_web_start_data_migration_state(monkeypatch):
    import json

    from cmp_api.config import settings
    from cmp_api.runtime_report import report

    value = {
        "source": {"access": "KUBECONFIG", "kubeconfig_ref": "src", "namespace": "app",
                   "pod": "db-0", "peer_host": "10.0.0.1", "dbname": "app"},
        "app": {"namespace": "app", "deployment": "app", "url_secret": "db-url"},
        "settings": {"image": "cmp-cutover:test"},
    }  # fmt: skip
    monkeypatch.setattr(settings, "web_start_data_migration", None)
    assert report()["web_start_data_migration"] == "off"
    monkeypatch.setattr(settings, "web_start_data_migration", json.dumps(value))
    assert report()["web_start_data_migration"] == "on"
    monkeypatch.setattr(settings, "web_start_data_migration", '{"source": {}}')
    assert report()["web_start_data_migration"] == "error:DATA_MIGRATION_SETTINGS_INVALID"


def test_web_start_traffic_is_an_api_value_off_by_default():
    assert _env("api")["WEB_START_TRAFFIC"] == "${WEB_START_TRAFFIC:-}"
    assert "WEB_START_TRAFFIC" not in _env("worker")
    assert _runtime_example()["WEB_START_TRAFFIC"] == ""  # needs the relay server's queue name
    assert "WEB_START_TRAFFIC" in (DEMO / "runtime-check.sh").read_text()


def test_api_runtime_report_names_the_web_start_traffic_state(monkeypatch):
    from cmp_api.config import settings
    from cmp_api.runtime_report import report

    monkeypatch.setattr(settings, "web_start_traffic", None)
    assert report()["web_start_traffic"] == "off"
    monkeypatch.setattr(settings, "web_start_traffic", '{"relay_queue": "relay-1"}')
    assert report()["web_start_traffic"] == "on"
    monkeypatch.setattr(settings, "web_start_traffic", '{"relay_queue": "bad queue"}')
    assert report()["web_start_traffic"] == "error:TRAFFIC_SETTINGS_INVALID"


def test_worker_clis_come_before_the_platform_source():
    """Deploy speed: a code-only change must not rebuild the ~1.4 GB CLI layer. The worker installs
    the pinned CLIs before copying the source, and both targets copy the same source set."""
    text = (DEMO / "Dockerfile").read_text()
    api_stage = text.split("FROM base AS api", 1)[1].split("FROM base AS worker", 1)[0]
    worker_stage = text.split("FROM base AS worker", 1)[1]
    assert worker_stage.index("sha256sum -c -") < worker_stage.index("COPY apps ./apps")
    copies = [line for line in api_stage.splitlines() if line.startswith("COPY ")]
    assert copies and all(line in worker_stage for line in copies)
    assert "COPY apps" not in text.split("FROM base AS api", 1)[0]  # base holds only the lock file


def test_worker_gets_data_migration_cluster_access_names_with_empty_defaults():
    """Provisioning 72 (#298, ADR-007 단계 6): the data-migration Activities read these two names.
    Empty keeps today's behavior: no profile (EKS fails closed) and no kubeconfig directory
    (`DbAccessNotConfigured`). The kubeconfig directory is a read-only secrets mount."""
    worker = _env("worker")
    assert worker["DATA_MIGRATION_AWS_PROFILE"] == "${DATA_MIGRATION_AWS_PROFILE:-}"
    assert worker["DATA_MIGRATION_KUBECONFIG_DIR"] == "${DATA_MIGRATION_KUBECONFIG_DIR:-}"
    assert "DATA_MIGRATION_AWS_PROFILE" not in _env("api")
    assert _mount_targets("worker")["/run/cmp/data-migration"] == "/opt/cmp/secrets/data-migration"
    assert (
        "/opt/cmp/secrets/data-migration:/run/cmp/data-migration:ro"
        in _compose()["services"]["worker"]["volumes"]
    )
    assert '"$secrets_dir/data-migration"' in (DEMO / "ensure-secrets.sh").read_text()
    example = _runtime_example()
    assert example["DATA_MIGRATION_AWS_PROFILE"] == ""
    assert example["DATA_MIGRATION_KUBECONFIG_DIR"] == ""
    check = (DEMO / "runtime-check.sh").read_text()
    for name in ("DATA_MIGRATION_AWS_PROFILE", "DATA_MIGRATION_KUBECONFIG_DIR"):
        assert name in check, name
    assert "data_migration_kubeconfig_files=" in check


def test_api_gets_the_tunnel_kube_api_switch_off_by_default():
    # Provisioning 78: TUNNEL_EXPOSE_KUBE_API in /opt/cmp/.env must reach the api (ticket 73).
    env = _env("api")
    assert env["TUNNEL_EXPOSE_KUBE_API"] == "${TUNNEL_EXPOSE_KUBE_API:-false}"
    assert env["TUNNEL_KUBE_API_PORT"] == "${TUNNEL_KUBE_API_PORT:-6443}"
    assert "TUNNEL_EXPOSE_KUBE_API" not in _env("worker")


def test_api_and_worker_images_are_settable_and_default_to_the_local_build_names():
    """Image deploy: CMP_*_IMAGE selects the registry tag; unset keeps the name compose gives a local build."""
    services = _compose()["services"]
    assert services["api"]["image"] == "${CMP_API_IMAGE:-cmp-demo-api}"
    assert services["worker"]["image"] == "${CMP_WORKER_IMAGE:-cmp-demo-worker}"
    assert "image" not in services["web"]  # the web is always built on the host
    assert "healthcheck" in services["api"]  # `up --wait` gates the api swap on it


def test_start_script_branches_to_image_mode_only_when_opted_in():
    start = (DEMO / "start.sh").read_text()
    assert "^CMP_DEPLOY_MODE=image" in start
    assert "start-image.sh" in start
    # The branch comes after secrets/guard setup and before the build flow, which stays as is.
    assert (
        start.index("build-binfmt.sh")
        < start.index("start-image.sh")
        < start.index("build api worker")
    )


def test_image_start_script_pulls_before_touching_anything_and_guards_the_worker():
    script = (DEMO / "start-image.sh").read_text()
    assert "pull --quiet api worker" in script
    assert script.index("pull --quiet api worker") < script.index("up -d platform-db")
    assert script.index("alembic upgrade head") < script.index("up -d --no-build --wait")
    assert "ExecutionStatus" in script and "CMP_WORKER_SWAP" in script
    assert "set -euo pipefail" in script
    assert ". /opt/cmp/.env" not in script and "set -a" not in script  # .env is never sourced
    syntax = subprocess.run(["bash", "-n", str(DEMO / "start-image.sh")], check=False)
    assert syntax.returncode == 0


def test_release_web_takes_the_deploy_lock():
    script = (DEMO / "release-web.sh").read_text()
    assert "/opt/cmp/deploy.lock" in script and "flock" in script
    assert script.index("flock -w") < script.index("docker compose --env-file")


def test_build_images_workflow_is_main_only_and_uses_only_the_github_token():
    text = (ROOT / ".github" / "workflows" / "build-images.yml").read_text()
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["branches"] == ["main"] and "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read", "packages": "write"}
    assert "secrets.GITHUB_TOKEN" in text and text.count("secrets.") == 1
    assert "GITHUB_SHA:0:12" in text


def test_activate_waits_for_the_lock_and_refuses_while_workflows_run():
    script = (DEMO / "activate.sh").read_text()
    assert "flock -w 900 9" in script and "flock -n" not in script
    # The running-workflow guard comes before start.sh, so a refused deploy touches nothing.
    assert 'ExecutionStatus="Running"' in script
    assert script.index("ExecutionStatus") < script.index('bash "$release_dir/infra/demo/start.sh"')
    assert '"$mode" != force' in script
    syntax = subprocess.run(["bash", "-n", str(DEMO / "activate.sh")], check=False)
    assert syntax.returncode == 0


def test_activate_keeps_failure_logs_and_a_fingerprint_ledger_without_values():
    script = (DEMO / "activate.sh").read_text()
    # The failed log is copied before the rollback overwrites the shared log files.
    assert script.index("failed-deploy-") < script.index("last-rollback.log")
    assert "deploy-history.log" in script and "sha256sum" in script
    assert "docker image prune -f" in script and "prune -a" not in script
    web = (DEMO / "release-web.sh").read_text()
    assert "deploy-history.log" in web and "prune -a" not in web


def test_release_deploys_only_main_with_the_control_script_from_main():
    script = (DEMO / "release.sh").read_text()
    assert 'git merge-base --is-ancestor "$revision" "$main_ref"' in script
    assert "main_ref=origin/main" in script and "CMP_ALLOW_UNMERGED" in script
    assert 'git show "$main_ref:infra/demo/activate.sh"' in script
    assert "aws s3 cp infra/demo/activate.sh" not in script  # never the working tree's copy
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy-demo.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["force"]["default"] is False


def test_start_script_gates_on_the_public_entry_when_it_is_enabled():
    start = (DEMO / "start.sh").read_text()
    assert '--resolve "$host:443:127.0.0.1"' in start
    assert start.index('echo "CMP demo API healthy"') < start.index("CMP demo public entry healthy")
