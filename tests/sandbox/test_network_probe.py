"""Ticket 30: the Sandbox service runs a probe from a short-lived Pod and always cleans up.

A Pod with the identity of an allowed source (the same instance label) must reach the database;
an outsider Pod has none, and the Namespace's default-deny would block its egress anyway, so the
outsider also gets a temporary egress allowance to the database. That way only the database's own
ingress rule decides. Seam: `SandboxService` with the recording `FakeRunner`.
"""

import json

import pytest
from cmp_worker.sandbox_deploy import SandboxError
from cmp_worker.sandbox_network import PROXY_IMAGE

from .test_deploy import FakeRunner, make_service
from .test_update import plan_with

PHASE = "jsonpath={.status.phase}"


def run_probe(tmp_path, phase, *, role="OUTSIDER", source=None, runner=None, **kw):
    runner = runner or FakeRunner(outputs={PHASE: phase.encode()})
    service = make_service(runner)
    plan = plan_with(service, tmp_path)
    runner.calls.clear()
    connected = service.network_probe(
        plan, role=role, source_resource_id=source, target_resource_id="orders-db", port=5432,
        pause=0, timeout=kw.pop("timeout", 5), **kw,
    )  # fmt: skip
    return connected, runner, plan


def applied(runner):
    documents = []
    for args, stdin in runner.calls:
        if stdin and "apply" in args:
            parsed = json.loads(stdin)
            documents += parsed["items"] if parsed.get("kind") == "List" else [parsed]
    return documents


def pod(runner):
    return next(d for d in applied(runner) if d["kind"] == "Pod")


def test_a_pod_that_connects_means_the_target_is_reachable(tmp_path):
    connected, _, _ = run_probe(tmp_path, "Succeeded", role="ALLOWED_CLIENT", source="orders-api")
    assert connected is True


def test_a_pod_that_cannot_connect_means_the_target_is_blocked(tmp_path):
    connected, _, _ = run_probe(tmp_path, "Failed")
    assert connected is False


def test_an_allowed_client_has_the_identity_of_its_source(tmp_path):
    _, runner, plan = run_probe(tmp_path, "Succeeded", role="ALLOWED_CLIENT", source="orders-api")
    labels = pod(runner)["metadata"]["labels"]
    assert labels["app.kubernetes.io/instance"] == plan.releases["orders-api"]
    assert labels["cmp.sandbox/probe"] == "allowed-client"
    assert not any(d["kind"] == "NetworkPolicy" for d in applied(runner))


def test_an_outsider_has_no_identity_and_a_temporary_egress_to_the_database_only(tmp_path):
    _, runner, plan = run_probe(tmp_path, "Failed")
    labels = pod(runner)["metadata"]["labels"]
    assert "app.kubernetes.io/instance" not in labels and labels["cmp.sandbox/probe"] == "outsider"
    (policy,) = [d for d in applied(runner) if d["kind"] == "NetworkPolicy"]
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": {"cmp.sandbox/probe": "outsider"}}
    assert spec["policyTypes"] == ["Egress"]
    (rule,) = spec["egress"]
    assert rule["to"] == [
        {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": plan.releases["orders-db"]}}}
    ]
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]


def test_the_pod_is_small_unprivileged_and_carries_no_secret(tmp_path):
    _, runner, plan = run_probe(tmp_path, "Succeeded", role="ALLOWED_CLIENT", source="orders-api")
    spec = pod(runner)["spec"]
    container = spec["containers"][0]
    assert container["image"] == PROXY_IMAGE and spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["command"][-2:] == [plan.releases["orders-db"], "5432"]
    assert "env" not in container and "envFrom" not in container
    assert "generated-db-password" not in json.dumps(runner.calls, default=str)


def test_the_pod_and_the_temporary_policy_are_deleted_after_the_probe(tmp_path):
    _, runner, _ = run_probe(tmp_path, "Failed")
    deletes = [a for a, _ in runner.calls if "delete" in a]
    assert any("pod" in a for a in deletes) and any("networkpolicy" in a for a in deletes)


def test_they_are_deleted_even_when_waiting_for_the_pod_gives_up(tmp_path):
    runner = FakeRunner(outputs={PHASE: b"Pending"})
    service = make_service(runner)
    plan = plan_with(service, tmp_path)
    runner.calls.clear()
    with pytest.raises(SandboxError) as caught:
        service.network_probe(
            plan, role="OUTSIDER", source_resource_id=None, target_resource_id="orders-db",
            port=5432, pause=0, timeout=0,
        )  # fmt: skip
    assert str(caught.value) == "PROBE_TIMEOUT"
    deletes = [a for a, _ in runner.calls if "delete" in a]
    assert any("pod" in a for a in deletes) and any("networkpolicy" in a for a in deletes)


def test_an_unknown_target_or_source_is_refused_before_anything_is_created(tmp_path):
    service = make_service(FakeRunner())
    plan = plan_with(service, tmp_path)
    for kwargs in (
        {"role": "OUTSIDER", "source_resource_id": None, "target_resource_id": "ghost"},
        {
            "role": "ALLOWED_CLIENT",
            "source_resource_id": "ghost",
            "target_resource_id": "orders-db",
        },
    ):
        with pytest.raises(SandboxError) as caught:
            service.network_probe(plan, port=5432, pause=0, timeout=1, **kwargs)
        assert str(caught.value) == "PROBE_TARGET_UNKNOWN"


def test_the_suite_runner_hands_each_probe_step_to_the_service_with_its_plan():
    from cmp_testsuite.model import ProbeStep
    from cmp_worker.activities.sandbox_loop import probe_runner

    calls = []

    class Service:
        def network_probe(self, plan, **kwargs):
            calls.append((plan, kwargs))
            return True

    step = ProbeStep(
        role="ALLOWED_CLIENT", source_resource_id="api", target_resource_id="db", port=5432,
        expect="CONNECTS",
    )  # fmt: skip
    assert probe_runner(Service(), "the-plan")(step) is True
    assert calls == [
        (
            "the-plan",
            {
                "role": "ALLOWED_CLIENT",
                "source_resource_id": "api",
                "target_resource_id": "db",
                "port": 5432,
            },
        )
    ]
