# ruff: noqa: F811 - `service` is a fixture imported from test_k3d and used by name
"""Ticket 38: the red-team executor on the real `cmp-sandbox` k3d cluster (tickets 33, 38).

Only proves what a fake cluster cannot: that the cluster really enforces the executor's policies,
that the executor reaches the application and nothing else, and that everything is gone afterwards.
No model and no key. Opt in like the other k3d tests:

  CMP_SANDBOX_KUBECONFIG=~/.kube/cmp-sandbox.yaml uv run pytest tests/integration/sandbox -m k3d -k redteam
"""

import json
import time
import uuid

import pytest
from cmp_worker.sandbox_deploy import SandboxError
from cmp_worker.sandbox_isolation import NOT_ENFORCED, RedTeamIsolation
from test_k3d import (  # noqa: F401 - `service` is a fixture
    KUBECONFIG,
    PODINFO,
    POSTGRES,
    SandboxCapabilities,
    SandboxImages,
    kubectl,
    requirements,
    service,
)

pytestmark = [
    pytest.mark.k3d,
    pytest.mark.skipif(not KUBECONFIG, reason="set CMP_SANDBOX_KUBECONFIG to run the k3d tests"),
]

ROLE = "cmp.sandbox/role=redteam-executor"


def sandbox(service, tmp_path):
    base = requirements().model_dump(mode="json")
    base["network_intent"] = [
        {"source_resource_id": "demo-api", "destination_resource_id": "demo-db", "port": 5432},
    ]
    from cmp_contracts.provisioning_v2 import RequirementsV2

    env_file = tmp_path / ".env"
    env_file.write_text("FEATURE_X=k3d-redteam-value\n")
    plan = service.prepare_sandbox(
        RequirementsV2.model_validate(base),
        migration_id="it-mig",
        run_id="rt-" + uuid.uuid4().hex[:8],
        images=SandboxImages(database=POSTGRES, applications={"demo-api": PODINFO}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=["FEATURE_X"],
        env_file=env_file,
        network=True,
    )
    service.apply_sandbox(plan)
    service.apply_network(plan)
    return plan


def dial(namespace, pod, host, port, *, path="/healthz"):
    """What the executor's own Python sees: the HTTP status, or the error that stopped it."""
    code = (
        "import urllib.request,sys\n"
        f"try:\n r=urllib.request.urlopen('http://{host}:{port}{path}',timeout=4);print('HTTP',r.status)\n"
        "except Exception as e:\n print('ERR',type(e).__name__)\n"
    )
    done = kubectl("exec", "-n", namespace, pod, "--", "python", "-c", code)
    return (done.stdout + done.stderr).strip()


def pods(namespace):
    out = kubectl("get", "pods", "-n", namespace, "-l", ROLE, "-o", "name")
    return [line for line in out.stdout.splitlines() if line]


def test_the_executor_reaches_only_the_application_and_leaves_nothing_behind(service, tmp_path):
    plan = sandbox(service, tmp_path)
    isolation = RedTeamIsolation(service.cluster, deadline_seconds=300)
    try:
        with isolation.session(plan, target_resource_id="demo-api") as executor:
            assert executor.namespace == plan.namespace and executor.port == 9898
            # The application answers through the one allowed path.
            assert (
                dial(plan.namespace, executor.name, executor.target_ip, executor.port) == "HTTP 200"
            )
            # Nothing else does: the database, the cluster API, the metadata address, the internet.
            db_ip = kubectl(
                "get", "service", plan.releases["demo-db"], "-n", plan.namespace,
                "-o", "jsonpath={.spec.clusterIP}",
            ).stdout  # fmt: skip
            api_ip = kubectl(
                "get", "service", "kubernetes", "-n", "default", "-o", "jsonpath={.spec.clusterIP}"
            ).stdout
            for host, port in (
                (db_ip, 5432),
                (api_ip, 443),
                ("169.254.169.254", 80),
                ("1.1.1.1", 80),
            ):
                assert dial(plan.namespace, executor.name, host, port).startswith("ERR"), host
            # No name resolution either: DNS is not a way out.
            assert dial(plan.namespace, executor.name, "example.com", 80).startswith("ERR")
            # No token, none of the application's environment, and no Service of the Sandbox named
            # in it. (`KUBERNETES_SERVICE_*` is injected by Kubernetes into every Pod; the address
            # it names is the one proved unreachable above and there is no token to use on it.)
            env = kubectl("exec", "-n", plan.namespace, executor.name, "--", "env").stdout
            assert "FEATURE_X" not in env and "k3d-redteam-value" not in env
            assert "APP_DEMO_API" not in env and "DB_DEMO_DB" not in env
            token = kubectl(
                "exec", "-n", plan.namespace, executor.name, "--", "ls",
                "/var/run/secrets/kubernetes.io/serviceaccount",
            )  # fmt: skip
            assert token.returncode != 0
            assert executor.name in " ".join(pods(plan.namespace))
        # Every way out of the session removed the Pods and the temporary policy, and the original
        # DNS rule is back for the other Pods.
        for _ in range(30):  # the Pods are deleted without waiting; give the kubelet a moment
            if not pods(plan.namespace):
                break
            time.sleep(1)
        assert pods(plan.namespace) == []
        policies = json.loads(
            kubectl("get", "networkpolicy", "-n", plan.namespace, "-o", "json").stdout
        )["items"]
        names = {p["metadata"]["name"] for p in policies}
        assert "cmp-redteam-egress" not in names and "cmp-allow-dns" in names
    finally:
        service.teardown_sandbox(plan.namespace)


def test_a_policy_that_lets_the_executor_out_stops_the_run_before_any_executor_exists(
    service, tmp_path
):
    plan = sandbox(service, tmp_path)
    loose = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "it-loose-egress", "namespace": plan.namespace},
        "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
    }
    try:
        kubectl_apply(plan.namespace, loose)
        with (
            pytest.raises(SandboxError) as caught,
            RedTeamIsolation(service.cluster, deadline_seconds=300).session(
                plan, target_resource_id="demo-api"
            ),
        ):
            pytest.fail("an executor was handed out although the isolation does not hold")
        assert str(caught.value) == NOT_ENFORCED
        assert pods(plan.namespace) == []
    finally:
        service.teardown_sandbox(plan.namespace)


def kubectl_apply(namespace, manifest):
    import subprocess
    from pathlib import Path

    KC = KUBECONFIG
    from cmp_worker.sandbox_deploy import SANDBOX_CONTEXT

    done = subprocess.run(
        ["kubectl", "--kubeconfig", str(Path(KC).expanduser()), "--context", SANDBOX_CONTEXT,
         "apply", "-f", "-"],
        input=json.dumps(manifest), capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr


def test_the_probe_tool_reads_the_application_through_the_executor_and_nothing_else(
    service, tmp_path
):
    from cmp_agents.redteam_agent import ToolCall
    from cmp_worker.redteam_tools import PROBE_GET, make_dispatch

    plan = sandbox(service, tmp_path)
    try:
        with RedTeamIsolation(service.cluster, deadline_seconds=300).session(
            plan, target_resource_id="demo-api"
        ) as executor:
            dispatch = make_dispatch(service.cluster, executor, known_secrets=["k3d-redteam-value"])
            health = json.loads(dispatch(ToolCall(tool=PROBE_GET, path="/healthz")).text)
            assert health["status"] == 200
            info = json.loads(dispatch(ToolCall(tool=PROBE_GET, path="/api/info")).text)
            assert info["status"] == 200 and "hostname" in info["body"]
            missing = json.loads(dispatch(ToolCall(tool=PROBE_GET, path="/no-such-page")).text)
            assert missing["status"] == 404
    finally:
        service.teardown_sandbox(plan.namespace)
