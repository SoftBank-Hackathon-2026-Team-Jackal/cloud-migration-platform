"""Ticket 15 (#69): the on-premises Agent install Chart, rendered with real Helm."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "infra/helm/onprem-agent"
HELM = os.environ.get("HELM_BIN", "helm")
DIGEST = "sha256:" + "b" * 64


def values(**override):
    base = {
        "agentId": "agent-a",
        "platform": {"url": "https://platform.example.com"},
        "enrollmentToken": "cmp_enroll_once",
        "image": {"digest": DIGEST},
    }
    for key, value in override.items():
        base[key] = value
    return base


def render(tmp_path, data):
    path = tmp_path / "values.json"
    path.write_text(json.dumps(data))
    return subprocess.run(
        [HELM, "template", "cmp-agent", str(CHART), "-n", "cmp-system", "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def documents(tmp_path, data):
    result = render(tmp_path, data)
    assert result.returncode == 0, result.stderr
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def deployment(docs):
    return next(doc for (kind, _), doc in docs.items() if kind == "Deployment")


def container(pod, name="agent"):
    return next(c for c in pod["containers"] if c["name"] == name)


def test_single_agent_runs_in_cluster_with_its_service_account(tmp_path):
    docs = documents(tmp_path, values())
    agent = deployment(docs)
    spec = agent["spec"]
    assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}
    pod = spec["template"]["spec"]
    agent_container = container(pod)
    assert (
        agent_container["image"]
        == f"ghcr.io/softbank-hackathon-2026-team-jackal/cmp-onprem-agent@{DIGEST}"
    )
    assert agent_container["args"][:6] == [
        "--platform-url",
        "https://platform.example.com",
        "--agent-id",
        "agent-a",
        "--in-cluster",
        "--namespace",
    ]
    service_account = pod["serviceAccountName"]
    assert ("ServiceAccount", service_account) in docs
    assert pod["terminationGracePeriodSeconds"] > 900  # longer than the Helm step timeout


def test_container_is_locked_down(tmp_path):
    pod = deployment(documents(tmp_path, values()))["spec"]["template"]["spec"]
    agent_container = container(pod)
    context = agent_container["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert any(mount["mountPath"] == "/tmp" for mount in agent_container["volumeMounts"])


def test_rbac_is_explicit_without_wildcards(tmp_path):
    docs = documents(tmp_path, values())
    [role] = [doc for (kind, _), doc in docs.items() if kind == "ClusterRole"]
    for rule in role["rules"]:
        assert "*" not in rule["verbs"] and "*" not in rule["resources"]
        assert "*" not in rule.get("apiGroups", [])
    granted = {
        (group, resource): set(rule["verbs"])
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
    }
    # list: tunnel discovery (17); delete: environment teardown with delete_data (52, D8)
    assert granted[("", "namespaces")] == {"get", "list", "create", "delete"}
    # Ticket 52 (D16): no `watch` anywhere since the Agent stopped using `helm --wait` (51).
    assert all("watch" not in rule["verbs"] for rule in role["rules"])
    assert granted[("storage.k8s.io", "storageclasses")] == {"get"}
    assert granted[("", "pods/exec")] == {"create"}
    assert granted[("", "services/proxy")] == {"get"}
    # Ticket 51: delete only (stuck StatefulSet Pod after rollback); never create/update/exec.
    pod_verbs = set().union(
        *(
            set(r["verbs"])
            for r in role["rules"]
            if "pods" in r["resources"] and r["apiGroups"] == [""]
        )
    )
    # create: the default Network Preflight probe image (52, D12)
    assert pod_verbs == {"get", "list", "create", "delete"}
    # Ticket 29 (I8): pull Secret linked to the default ServiceAccount; no delete/update.
    assert granted[("", "serviceaccounts")] == {"get", "create", "patch"}
    [binding] = [doc for (kind, _), doc in docs.items() if kind == "ClusterRoleBinding"]
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert binding["subjects"][0]["namespace"] == "cmp-system"


def test_enrollment_token_secret_only_when_given_and_credential_is_not_managed(tmp_path):
    docs = documents(tmp_path, values())
    secret = docs[("Secret", "cmp-agent-enrollment")]
    assert secret["stringData"] == {"token": "cmp_enroll_once"}
    assert not any(kind == "Secret" and name == "cmp-agent-credential" for kind, name in docs)
    upgraded = documents(tmp_path, values(enrollmentToken=""))
    assert not any(kind == "Secret" for kind, _ in upgraded)


def test_token_never_appears_in_the_pod_spec(tmp_path):
    agent = deployment(documents(tmp_path, values()))
    assert "cmp_enroll_once" not in json.dumps(agent)


def test_private_ca_bundle_is_trusted_via_ssl_cert_file(tmp_path):
    docs = documents(tmp_path, values(platform={"url": "https://p.test", "caBundle": "PEM"}))
    config = next(doc for (kind, _), doc in docs.items() if kind == "ConfigMap")
    assert config["data"] == {"ca.crt": "PEM"}
    pod = deployment(docs)["spec"]["template"]["spec"]
    for name in ("agent", "tunnel"):  # both talk to the platform
        env = {item["name"]: item["value"] for item in container(pod, name)["env"]}
        assert env["SSL_CERT_FILE"] == "/etc/cmp-agent/ca/ca.crt"
    no_ca = documents(tmp_path, values())
    assert not any(kind == "ConfigMap" for kind, _ in no_ca)


@pytest.mark.parametrize(
    "bad",
    [
        {"platform": {"url": "http://platform.example.com"}},
        {"agentId": "Agent_A"},
        {"agentId": ""},
        {"image": {"digest": "latest"}},
        {"image": {"digest": "", "tag": ""}},
    ],
)
def test_schema_rejects_unsafe_or_missing_values(tmp_path, bad):
    data = values(**bad)
    result = render(tmp_path, data)
    assert result.returncode != 0


def test_tag_is_allowed_for_local_builds(tmp_path):
    data = values(image={"repository": "cmp-onprem-agent", "tag": "dev", "digest": ""})
    pod = deployment(documents(tmp_path, data))["spec"]["template"]["spec"]
    assert {c["image"] for c in pod["containers"]} == {"cmp-onprem-agent:dev"}


# --- release packaging (H4) ------------------------------------------------------------------


def test_release_package_pins_version_and_image_digest(tmp_path):
    import importlib.util
    import tarfile

    spec = importlib.util.spec_from_file_location(
        "package_agent_chart", ROOT / "scripts/package_agent_chart.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = module.package("0.1.0", DIGEST, tmp_path / "dist", "ghcr.io/example/cmp-onprem-agent")
    assert path.name == "onprem-agent-0.1.0.tgz"
    with tarfile.open(path) as archive:
        chart = yaml.safe_load(archive.extractfile("onprem-agent/Chart.yaml"))
        packaged = yaml.safe_load(archive.extractfile("onprem-agent/values.yaml"))
    assert chart["version"] == "0.1.0" and chart["appVersion"] == "0.1.0"
    assert packaged["image"]["digest"] == DIGEST
    assert packaged["image"]["repository"] == "ghcr.io/example/cmp-onprem-agent"
    # The source chart stays unpinned.
    assert yaml.safe_load((CHART / "values.yaml").read_text())["image"]["digest"] == ""
    with pytest.raises(ValueError, match="INVALID_IMAGE_DIGEST"):
        module.package("0.1.0", "latest", tmp_path / "dist")
    with pytest.raises(ValueError, match="INVALID_VERSION"):
        module.package("v0.1", DIGEST, tmp_path / "dist")


# --- ticket 17 (#71): tunnel container -----------------------------------------------------------


def test_tunnel_container_has_only_network_admin_and_no_host_network(tmp_path):
    pod = deployment(documents(tmp_path, values()))["spec"]["template"]["spec"]
    tunnel = container(pod, "tunnel")
    assert tunnel["image"] == container(pod)["image"]
    assert tunnel["command"] == ["python", "-m", "cmp_providers.agent_tunnel"]
    assert tunnel["args"] == [
        "--platform-url",
        "https://platform.example.com",
        "--agent-id",
        "agent-a",
        "--namespace",
        "cmp-system",
        "--mode",
        "auto",
        "--interval-seconds",
        "15",
        "--wake-file",
        "/var/run/cmp-wake/job",
    ]
    context = tunnel["securityContext"]
    assert context["capabilities"] == {"drop": ["ALL"], "add": ["NET_ADMIN", "NET_BIND_SERVICE"]}
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context.get("privileged") is not True
    assert pod.get("hostNetwork") is not True
    assert all("ports" not in c and "hostPort" not in json.dumps(c) for c in pod["containers"])
    mounts = {m["mountPath"] for m in tunnel["volumeMounts"]}
    assert {"/tmp", "/var/run/wireguard", "/dev/net/tun"} <= mounts
    tun = next(v for v in pod["volumes"] if v["name"] == "tun")
    assert tun["hostPath"] == {"path": "/dev/net/tun", "type": "CharDevice"}
    # The agent container keeps its own /tmp (generated kubeconfig is owner-only per container).
    agent_tmp = next(m for m in container(pod)["volumeMounts"] if m["mountPath"] == "/tmp")
    tunnel_tmp = next(m for m in tunnel["volumeMounts"] if m["mountPath"] == "/tmp")
    assert agent_tmp["name"] != tunnel_tmp["name"]


def test_agent_wakes_the_tunnel_through_a_shared_memory_volume(tmp_path):
    """Ticket 66 (#283): the agent container writes the signal file the tunnel container reads."""
    pod = deployment(documents(tmp_path, values()))["spec"]["template"]["spec"]
    agent, tunnel = container(pod), container(pod, "tunnel")
    assert agent["args"][-2:] == ["--tunnel-wake-file", "/var/run/cmp-wake/job"]
    for item in (agent, tunnel):
        mount = next(m for m in item["volumeMounts"] if m["mountPath"] == "/var/run/cmp-wake")
        assert mount["name"] == "tunnel-wake" and not mount.get("readOnly")
    volume = next(v for v in pod["volumes"] if v["name"] == "tunnel-wake")
    assert volume["emptyDir"] == {"medium": "Memory", "sizeLimit": "1Mi"}


def test_disabled_tunnel_has_no_wake_signal(tmp_path):
    pod = deployment(documents(tmp_path, values(tunnel={"enabled": False})))["spec"]["template"]
    assert "--tunnel-wake-file" not in container(pod["spec"])["args"]
    assert all(v["name"] != "tunnel-wake" for v in pod["spec"]["volumes"])


def test_kernel_mode_does_not_mount_the_tun_device(tmp_path):
    data = values(tunnel={"mode": "kernel"})
    pod = deployment(documents(tmp_path, data))["spec"]["template"]["spec"]
    assert "--mode" in container(pod, "tunnel")["args"]
    assert all(v["name"] != "tun" for v in pod["volumes"])


def test_tunnel_can_be_disabled(tmp_path):
    pod = deployment(documents(tmp_path, values(tunnel={"enabled": False})))["spec"]["template"]
    assert [c["name"] for c in pod["spec"]["containers"]] == ["agent"]


def test_kube_api_forwarding_is_off_unless_the_cluster_owner_turns_it_on(tmp_path):
    """Ticket 73 (#299): the platform names the port, the chart value lets the Agent forward it."""
    default = documents(tmp_path, values())
    pod = deployment(default)["spec"]["template"]["spec"]
    assert "--expose-kube-api" not in container(pod, "tunnel")["args"]
    enabled = documents(tmp_path, values(tunnel={"exposeKubeApi": True}))
    pod = deployment(enabled)["spec"]["template"]["spec"]
    assert "--expose-kube-api" in container(pod, "tunnel")["args"]
    # TCP relay to `kubernetes.default.svc` only: no extra RBAC.
    assert _cluster_rules(enabled) == _cluster_rules(default)


def _cluster_rules(docs):
    return [
        r for (kind, _), d in docs.items() if kind in ("ClusterRole", "Role") for r in d["rules"]
    ]


@pytest.mark.parametrize(
    "bad", [{"mode": "tcp"}, {"intervalSeconds": 0}, {"extra": 1}, {"exposeKubeApi": "yes"}]
)
def test_tunnel_values_are_validated(tmp_path, bad):
    assert render(tmp_path, values(tunnel=bad)).returncode != 0


# --- MNP-01 (#51, N3): Network Preflight probe -------------------------------------------------

PROBE = "postgres@sha256:" + "c" * 64


def _pod_rules(docs):
    [role] = [doc for (kind, _), doc in docs.items() if kind == "ClusterRole"]
    return [
        set(rule["verbs"])
        for rule in role["rules"]
        if rule["apiGroups"] == [""] and rule["resources"] in (["pods"], ["pods/log"])
    ], role


DEFAULT_PROBE = (
    "docker.io/library/postgres@sha256:"
    "0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab"
)


def test_preflight_runs_by_default_with_the_pinned_public_probe_image(tmp_path):
    """Ticket 52 (D12, N13): installing without values must not refuse NETWORK_PREFLIGHT."""
    docs = documents(tmp_path, values())
    rules, _ = _pod_rules(docs)
    assert rules == [{"delete"}, {"create", "delete"}, {"get"}]
    args = container(deployment(docs)["spec"]["template"]["spec"])["args"]
    assert args[args.index("--preflight-image") + 1] == DEFAULT_PROBE
    assert args[args.index("--preflight-allowed-images") + 1] == DEFAULT_PROBE
    chart_values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert chart_values["networkPreflight"]["image"] == DEFAULT_PROBE


def test_preflight_can_be_switched_off_and_then_grants_no_pod_create(tmp_path):
    docs = documents(tmp_path, values(networkPreflight={"image": ""}))
    rules, role = _pod_rules(docs)
    assert rules == [{"delete"}]  # ticket 51 rollback recovery only
    pod_verbs = {
        verb for rule in role["rules"] if "pods" in rule["resources"] for verb in rule["verbs"]
    }
    assert "create" not in pod_verbs
    assert (
        "--preflight-image" not in container(deployment(docs)["spec"]["template"]["spec"])["args"]
    )


def _env(pod, name):
    return {item["name"]: item.get("value") for item in container(pod, name).get("env", [])}


def test_no_proxy_settings_by_default(tmp_path):
    pod = deployment(documents(tmp_path, values()))["spec"]["template"]["spec"]
    for name in ("agent", "tunnel"):
        assert not {"HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"} & set(_env(pod, name))


def test_proxy_reaches_both_containers_and_keeps_cluster_traffic_direct(tmp_path):
    """Ticket 52 (D13, C-08)."""
    proxy = {"https": "http://proxy.corp.example:3128"}
    pod = deployment(documents(tmp_path, values(proxy=proxy)))["spec"]["template"]["spec"]
    for name in ("agent", "tunnel"):
        env = _env(pod, name)
        assert env["HTTPS_PROXY"] == env["https_proxy"] == proxy["https"]
        assert "HTTP_PROXY" not in env
        no_proxy = env["NO_PROXY"].split(",")
        assert env["no_proxy"] == env["NO_PROXY"]
        assert {".svc", ".cluster.local", "10.43.0.0/16", "localhost"} <= set(no_proxy)
    custom = values(proxy=proxy | {"http": "http://p:3128", "noProxy": "10.0.0.0/8,.internal"})
    env = _env(deployment(documents(tmp_path, custom))["spec"]["template"]["spec"], "agent")
    assert env["HTTP_PROXY"] == "http://p:3128" and env["NO_PROXY"] == "10.0.0.0/8,.internal"


@pytest.mark.parametrize(
    "bad",
    [{"https": "proxy:3128"}, {"https": "http://a b"}, {"noProxy": "a, b"}, {"other": "x"}],
)
def test_proxy_values_are_validated(tmp_path, bad):
    assert render(tmp_path, values(proxy=bad)).returncode != 0


def test_preflight_image_adds_minimal_pod_rules_and_agent_args(tmp_path):
    docs = documents(tmp_path, values(networkPreflight={"image": PROBE}))
    rules, _ = _pod_rules(docs)
    # ticket 51 pods delete; preflight pods create/delete; pods/log (no exec, no update)
    assert rules == [{"delete"}, {"create", "delete"}, {"get"}]
    args = container(deployment(docs)["spec"]["template"]["spec"])["args"]
    assert args[args.index("--preflight-image") + 1] == PROBE
    assert args[args.index("--preflight-allowed-images") + 1] == PROBE


@pytest.mark.parametrize(
    "bad",
    [{"image": "postgres:16-alpine"}, {"allowedImages": ["postgres:16"]}, {"other": 1}],
)
def test_preflight_image_must_be_digest_pinned(tmp_path, bad):
    assert render(tmp_path, values(networkPreflight=bad)).returncode != 0


# --- ticket 42 (#177): private registry pull (imagePullSecrets) ----------------------------------


def test_no_image_pull_secrets_by_default(tmp_path):
    pod = deployment(documents(tmp_path, values()))["spec"]["template"]["spec"]
    assert "imagePullSecrets" not in pod  # public GHCR packages need no credential


def test_image_pull_secrets_apply_to_the_agent_pod(tmp_path):
    data = values(imagePullSecrets=[{"name": "cmp-agent-ghcr"}, {"name": "mirror-pull"}])
    docs = documents(tmp_path, data)
    pod = deployment(docs)["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "cmp-agent-ghcr"}, {"name": "mirror-pull"}]
    # Every Pod template the Chart renders references the Secret (agent + tunnel share one Pod).
    templates = [
        doc["spec"]["template"]["spec"]
        for doc in docs.values()
        if "template" in doc.get("spec", {})
    ]
    assert templates and all(
        t.get("imagePullSecrets") == pod["imagePullSecrets"] for t in templates
    )
    # The Chart only references a Secret the user created; it never stores registry credentials.
    assert not any(kind == "Secret" and name == "cmp-agent-ghcr" for kind, name in docs)


@pytest.mark.parametrize(
    "bad",
    [
        ["cmp-agent-ghcr"],  # objects with `name`, like the Kubernetes field
        [{"name": ""}],
        [{"name": "Bad_Name"}],
        [{"name": "ok", "password": "x"}],  # credentials never go through Chart values
        {"name": "ok"},
    ],
)
def test_image_pull_secrets_are_validated(tmp_path, bad):
    assert render(tmp_path, values(imagePullSecrets=bad)).returncode != 0


# --- ticket 42 (#177): anonymous package access check ---------------------------------------------


def _access_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_agent_package_access", ROOT / "scripts/check_agent_package_access.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry(public):
    """Anonymous GHCR behaviour: public packages get a token and a manifest, private ones 401/403."""
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        package = url.split("repository:", 1)[-1].split(":pull")[0] if "token?" in url else ""
        if "/token?" in url:
            name = package.split("/", 1)[1]
            return (200, b'{"token":"anon"}') if name in public else (401, b'{"errors":[]}')
        name = url.split("/v2/", 1)[1].split("/manifests/")[0].split("/", 1)[1]
        assert headers.get("Authorization") == "Bearer anon"
        assert "application/vnd.oci.image.index.v1+json" in headers["Accept"]
        return (200, b"{}") if name in public else (403, b"")

    return fetch, calls


def test_access_check_reports_public_packages():
    module = _access_module()
    fetch, calls = _registry({"cmp-onprem-agent", "charts/onprem-agent"})
    report = module.check("0.3.0", "SoftBank-Hackathon-2026-Team-Jackal", fetch=fetch)
    assert report == {"cmp-onprem-agent": "PUBLIC", "charts/onprem-agent": "PUBLIC"}
    assert any(
        url
        == "https://ghcr.io/v2/softbank-hackathon-2026-team-jackal/charts/onprem-agent/manifests/0.3.0"
        for url, _ in calls
    )
    assert all("Authorization" not in headers for url, headers in calls if "/token?" in url)


def test_access_check_reports_private_packages_and_fails():
    module = _access_module()
    fetch, _ = _registry({"cmp-onprem-agent"})
    report = module.check("0.3.0", "softbank-hackathon-2026-team-jackal", fetch=fetch)
    assert report == {"cmp-onprem-agent": "PUBLIC", "charts/onprem-agent": "PRIVATE"}
    assert module.main(["--version", "0.3.0"], fetch=fetch) == 1
    with pytest.raises(ValueError, match="INVALID_VERSION"):
        module.check("latest", "owner", fetch=fetch)


def test_release_notes_document_public_and_authenticated_install():
    workflow = yaml.safe_load((ROOT / ".github/workflows/agent-release.yml").read_text())
    steps = {step.get("name"): step for step in workflow["jobs"]["release"]["steps"]}
    notes = steps["Create GitHub Release"]["run"]
    assert "--create-namespace" in notes  # anonymous path stays the default command
    assert "helm registry login ghcr.io" in notes
    assert "create secret docker-registry cmp-agent-ghcr" in notes
    assert "imagePullSecrets[0].name=cmp-agent-ghcr" in notes
    check = steps["Check anonymous pull"]["run"]
    assert "scripts/check_agent_package_access.py" in check and "::warning::" in check
