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


def test_single_agent_runs_in_cluster_with_its_service_account(tmp_path):
    docs = documents(tmp_path, values())
    agent = deployment(docs)
    spec = agent["spec"]
    assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}
    pod = spec["template"]["spec"]
    [container] = pod["containers"]
    assert (
        container["image"]
        == f"ghcr.io/softbank-hackathon-2026-team-jackal/cmp-onprem-agent@{DIGEST}"
    )
    assert container["args"][:6] == [
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
    [container] = pod["containers"]
    context = container["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert any(mount["mountPath"] == "/tmp" for mount in container["volumeMounts"])


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
    assert granted[("", "namespaces")] == {"get", "create"}
    assert granted[("storage.k8s.io", "storageclasses")] == {"get"}
    assert granted[("", "pods/exec")] == {"create"}
    assert granted[("", "services/proxy")] == {"get"}
    assert "delete" not in granted[("", "namespaces")]
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
    [container] = deployment(docs)["spec"]["template"]["spec"]["containers"]
    env = {item["name"]: item["value"] for item in container["env"]}
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
    [container] = deployment(documents(tmp_path, data))["spec"]["template"]["spec"]["containers"]
    assert container["image"] == "cmp-onprem-agent:dev"


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
