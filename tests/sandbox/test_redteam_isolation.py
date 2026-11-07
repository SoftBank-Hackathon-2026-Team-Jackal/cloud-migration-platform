"""Ticket 33: the red-team executor is isolated before anything else happens.

Seams: pure manifests and policies judged by a small NetworkPolicy evaluator; the isolation
session on `SandboxService`'s cluster with the recording `FakeRunner`. Real enforcement is checked
separately on the k3d Sandbox cluster (`tests/integration/sandbox -m k3d -k redteam`).

Nothing here attacks anything: the verification Pod only tries to open TCP connections that the
policies must refuse, and the session refuses to hand out an executor unless every one failed.
"""

import json

import pytest
from cmp_providers import network_policy
from cmp_worker.sandbox_deploy import SandboxError
from cmp_worker.sandbox_input import SandboxSettings
from cmp_worker.sandbox_isolation import RedTeamIsolation
from cmp_worker.sandbox_network import PROXY_IMAGE, PROXY_NAME, build_manifests
from cmp_worker.sandbox_redteam import (
    REDTEAM_ROLE,
    REDTEAM_ROLE_LABEL,
    executor_pod,
    restricted_violations,
)
from pydantic import ValidationError

from .test_deploy import FakeRunner, make_service
from .test_update import plan_with

NS_LABELS = {"app.kubernetes.io/managed-by": "cmp-sandbox", "cmp.sandbox/run-id": "run1"}
INSTANCE = "app.kubernetes.io/instance"
FORBIDDEN = ("db", "proxy", "other_namespace", "kubernetes_api", "metadata", "internet", "dns")
ALL_CHECKS = ("app", *FORBIDDEN)


# --- a minimal NetworkPolicy evaluator (selectors, namespaces, ipBlock, ports) -----------------


def matches(selector, labels):
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        present = expression["key"] in labels
        operator = expression["operator"]
        if operator == "In" and labels.get(expression["key"]) not in expression["values"]:
            return False
        if operator == "DoesNotExist" and present:
            return False
        if operator == "Exists" and not present:
            return False
    return True


def peer_ok(peer, owner_ns, other):
    if "ipBlock" in peer:
        if other.get("pod"):
            return False
        import ipaddress

        ip = ipaddress.ip_address(other["ip"])
        block = peer["ipBlock"]
        return ip in ipaddress.ip_network(block["cidr"]) and not any(
            ip in ipaddress.ip_network(c) for c in block.get("except", [])
        )
    if not other.get("pod"):
        return False
    if "namespaceSelector" in peer:
        ns_ok = matches(peer["namespaceSelector"], {"kubernetes.io/metadata.name": other["ns"]})
    else:
        ns_ok = other["ns"] == owner_ns
    return ns_ok and matches(peer.get("podSelector") or {}, other["labels"])


def selecting(policies, pod, kind):
    return [
        p
        for p in policies
        if p["metadata"]["namespace"] == pod["ns"]
        and kind in p["spec"]["policyTypes"]
        and matches(p["spec"]["podSelector"], pod["labels"])
    ]


def egress_allowed(policies, source, destination, port, protocol="TCP"):
    chosen = selecting(policies, source, "Egress")
    if not chosen:
        return True
    for item in chosen:
        for rule in item["spec"].get("egress") or []:
            peers, ports = rule.get("to"), rule.get("ports")
            peer_match = not peers or any(peer_ok(p, source["ns"], destination) for p in peers)
            port_match = not ports or any(
                (x.get("protocol", "TCP"), x["port"]) == (protocol, port) for x in ports
            )
            if peer_match and port_match:
                return True
    return False


def pod(ns, **labels):
    return {"pod": True, "ns": ns, "labels": labels}


# --- fixtures -----------------------------------------------------------------------------------

NS = "cmp-sbx-demo-aaaa1111"
LABELS = {"app.kubernetes.io/managed-by": "cmp-sandbox", "cmp.sandbox/run-id": "run1"}
RESOURCES = [
    {"id": "orders-api", "kind": "APPLICATION", "release_name": "app-orders-api", "port": 8080},
    {"id": "orders-db", "kind": "POSTGRESQL", "release_name": "db-orders-db"},
]
RELEASES = {"orders-api": "app-orders-api", "orders-db": "db-orders-db"}
INTENTS = [
    {"source_resource_id": "orders-api", "destination_resource_id": "orders-db", "port": 5432}
]


def sandbox_policies(*, redteam=True):
    manifests = build_manifests(
        namespace=NS, labels=LABELS, resources=RESOURCES, releases=RELEASES, intents=INTENTS,
        allow=[], proxy_script="", redteam=redteam,
    )  # fmt: skip
    return [m for m in manifests if m["kind"] == "NetworkPolicy"]


def make_executor(run_id="run1", deadline=300):
    return executor_pod(NS, run_id, image=PROXY_IMAGE, deadline_seconds=deadline)


EXECUTOR = pod(
    NS, **{REDTEAM_ROLE_LABEL: REDTEAM_ROLE, "app.kubernetes.io/managed-by": "cmp-sandbox"}
)
APP = pod(NS, **{INSTANCE: "app-orders-api"})
DB = pod(NS, **{INSTANCE: "db-orders-db"})
PROXY = pod(NS, **{INSTANCE: PROXY_NAME})
KUBE_DNS = pod("kube-system", **{"k8s-app": "kube-dns"})
OTHER_APP = pod("cmp-sbx-someone-else", **{INSTANCE: "app-orders-api"})


# --- manifests ----------------------------------------------------------------------------------


def test_the_executor_is_unprivileged_secretless_bounded_and_pinned():
    manifest = make_executor()
    spec, labels = manifest["spec"], manifest["metadata"]["labels"]
    container = spec["containers"][0]
    assert labels[REDTEAM_ROLE_LABEL] == REDTEAM_ROLE
    assert INSTANCE not in labels and "app.kubernetes.io/name" not in labels
    assert spec["automountServiceAccountToken"] is False and spec["restartPolicy"] == "Never"
    assert spec["activeDeadlineSeconds"] == 300
    assert "env" not in container and "envFrom" not in container and "volumes" not in spec
    assert container["image"] == PROXY_IMAGE and "@sha256:" in container["image"]
    assert container["resources"]["limits"]["cpu"] and container["resources"]["limits"]["memory"]
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        assert not spec.get(flag)
    assert restricted_violations(manifest) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda s: s.update(hostNetwork=True), "hostNetwork"),
        (lambda s: s["containers"][0]["securityContext"].update(privileged=True), "privileged"),
        (
            lambda s: s["containers"][0]["securityContext"].update(runAsNonRoot=False),
            "runAsNonRoot",
        ),
        (
            lambda s: s["containers"][0]["securityContext"].update(
                capabilities={"add": ["NET_RAW"], "drop": ["ALL"]}
            ),
            "capabilities",
        ),
        (
            lambda s: s["containers"][0].update(ports=[{"containerPort": 80, "hostPort": 80}]),
            "hostPort",
        ),
        (lambda s: s["containers"][0]["securityContext"].pop("seccompProfile"), "seccomp"),
        (lambda s: s.update(automountServiceAccountToken=True), "serviceAccountToken"),
        (lambda s: s["containers"][0].update(env=[{"name": "X", "value": "y"}]), "env"),
    ],
)
def test_the_validator_flags_every_way_the_executor_could_become_less_isolated(change, expected):
    manifest = make_executor()
    change(manifest["spec"])
    assert any(expected in v for v in restricted_violations(manifest))


def test_executor_names_are_deterministic_per_run():
    assert make_executor("run1")["metadata"]["name"] == make_executor("run1")["metadata"]["name"]
    assert make_executor("run1")["metadata"]["name"] != make_executor("run2")["metadata"]["name"]


# --- policies, judged by the evaluator ---------------------------------------------------------


def test_the_executor_may_reach_the_application_port_and_nothing_else():
    policies = sandbox_policies()
    assert egress_allowed(policies, EXECUTOR, APP, 8080)
    for destination, port in (
        (APP, 5432), (APP, 22), (DB, 5432), (DB, 8080), (PROXY, 3128), (OTHER_APP, 8080),
        (KUBE_DNS, 53), ({"ip": "169.254.169.254"}, 80), ({"ip": "1.1.1.1"}, 443),
        ({"ip": "10.43.0.1"}, 443), ({"ip": "192.168.1.10"}, 8080),
    ):  # fmt: skip
        assert not egress_allowed(policies, EXECUTOR, destination, port), (destination, port)
    assert not egress_allowed(policies, EXECUTOR, KUBE_DNS, 53, "UDP")


def test_only_the_executors_own_policies_select_it():
    names = {p["metadata"]["name"] for p in selecting(sandbox_policies(), EXECUTOR, "Egress")}
    assert names == {"cmp-default-deny", "cmp-redteam-egress"}


def test_other_pods_keep_dns_when_the_executor_loses_it():
    policies = sandbox_policies()
    assert egress_allowed(policies, APP, KUBE_DNS, 53, "UDP")
    assert egress_allowed(policies, DB, KUBE_DNS, 53, "TCP")


def test_the_application_pod_gains_nothing_from_the_executors_policies():
    with_executor = sandbox_policies(redteam=True)
    without = sandbox_policies(redteam=False)
    for destination, port in ((DB, 5432), (DB, 8080), (PROXY, 3128), ({"ip": "1.1.1.1"}, 443)):
        assert egress_allowed(with_executor, APP, destination, port) == egress_allowed(
            without, APP, destination, port
        ), (destination, port)


def test_nothing_changes_for_existing_output_when_red_team_is_off():
    plain = build_manifests(
        namespace=NS, labels=LABELS, resources=RESOURCES, releases=RELEASES, intents=INTENTS,
        allow=[], proxy_script="",
    )  # fmt: skip
    assert plain == build_manifests(
        namespace=NS, labels=LABELS, resources=RESOURCES, releases=RELEASES, intents=INTENTS,
        allow=[], proxy_script="", redteam=False,
    )  # fmt: skip
    dns = next(m for m in plain if m["metadata"]["name"] == "cmp-allow-dns")
    assert dns["spec"]["podSelector"] == {}
    assert network_policy.allow_dns(NS, LABELS) == dns
    isolated = next(m for m in sandbox_policies() if m["metadata"]["name"] == "cmp-allow-dns")
    assert isolated["spec"]["podSelector"] == {
        "matchExpressions": [{"key": REDTEAM_ROLE_LABEL, "operator": "DoesNotExist"}]
    }


# --- the isolation session (recording runner) --------------------------------------------------


def isolation(
    tmp_path, *, verdicts=None, labels=NS_LABELS, running="", phase="Succeeded", **runner_kw
):
    verdicts = {name: name == "app" for name in ALL_CHECKS} | (verdicts or {})
    log = "\n".join(json.dumps({"check": k, "reachable": v}) for k, v in verdicts.items())
    runner = FakeRunner(
        outputs={
            "get namespace": json.dumps({"metadata": {"labels": labels}}).encode(),
            "get pods -A": running.encode(),
            "logs cmp-redteam-vf": log.encode(),
            "jsonpath={.status.phase}": phase.encode(),
            "service kubernetes": b"10.43.0.1",
            "service kube-dns": b"10.43.0.10",
            "service cmp-egress-proxy": b"10.43.0.30",
            "service db-": b"10.43.0.20",
            "service app-": b"10.43.0.40",
        }
        | runner_kw.pop("outputs", {})
    )
    service = make_service(runner)
    plan = plan_with(service, tmp_path)
    runner.calls.clear()
    return RedTeamIsolation(service.cluster, pause=0, timeout=2), plan, runner


def applied(runner):
    documents = []
    for args, stdin in runner.calls:
        if stdin and "apply" in args:
            parsed = json.loads(stdin)
            documents += parsed["items"] if parsed.get("kind") == "List" else [parsed]
    return documents


def deleted(runner):
    return [" ".join(a) for a, _ in runner.calls if "delete" in a]


def refused(guard, plan, target="orders-api"):
    """The error code a session ends with. No executor may ever be handed out in these cases."""
    with pytest.raises(SandboxError) as caught, guard.session(plan, target_resource_id=target):
        raise AssertionError("no executor may be handed out")
    return str(caught.value)


def test_a_proven_isolation_hands_out_an_executor_and_removes_everything_afterwards(tmp_path):
    guard, plan, runner = isolation(tmp_path)
    with guard.session(plan, target_resource_id="orders-api") as executor:
        assert executor.target_ip == "10.43.0.40" and executor.port == 8080
        kinds = [d["kind"] for d in applied(runner)]
        assert kinds.index("NetworkPolicy") < kinds.index("Pod")
        pods = [d for d in applied(runner) if d["kind"] == "Pod"]
        assert [p["metadata"]["name"] for p in pods][-1] == executor.name
        assert len(pods) == 2  # the verification Pod first, then the executor
    text = deleted(runner)
    assert any(f"pod {executor.name}" in c for c in text)
    assert any("networkpolicy cmp-redteam-egress" in c for c in text)
    dns = [d for d in applied(runner) if d["metadata"]["name"] == "cmp-allow-dns"]
    assert dns[-1]["spec"]["podSelector"] == {}  # the original DNS policy is back


@pytest.mark.parametrize("forbidden", FORBIDDEN)
def test_any_forbidden_target_that_answers_stops_everything_before_an_executor_exists(
    tmp_path, forbidden
):
    guard, plan, runner = isolation(tmp_path, verdicts={forbidden: True})
    assert refused(guard, plan) == "REDTEAM_ISOLATION_NOT_ENFORCED"
    executors = [
        d
        for d in applied(runner)
        if d["kind"] == "Pod" and "redteam-vf" not in d["metadata"]["name"]
    ]
    assert executors == []
    assert any("networkpolicy cmp-redteam-egress" in c for c in deleted(runner))


def test_a_missing_or_unreadable_verdict_counts_as_not_enforced(tmp_path):
    guard, plan, _ = isolation(
        tmp_path, outputs={"logs cmp-redteam-vf": b'{"check": "app"}\nnoise'}
    )
    assert refused(guard, plan) == "REDTEAM_ISOLATION_NOT_ENFORCED"


def test_a_verification_pod_that_never_finishes_counts_as_not_enforced(tmp_path):
    guard, plan, runner = isolation(tmp_path, phase="Pending")
    guard = RedTeamIsolation(guard.cluster, pause=0, timeout=0)
    assert refused(guard, plan) == "REDTEAM_ISOLATION_NOT_ENFORCED"
    assert any("networkpolicy cmp-redteam-egress" in c for c in deleted(runner))


def test_an_unreachable_application_is_reported_apart_from_a_leak(tmp_path):
    guard, plan, _ = isolation(tmp_path, verdicts={"app": False})
    assert refused(guard, plan) == "REDTEAM_TARGET_UNREACHABLE"


@pytest.mark.parametrize(
    "labels",
    [{}, {"cmp.sandbox/run-id": "other-run", "app.kubernetes.io/managed-by": "cmp-sandbox"},
     {"cmp.sandbox/run-id": "run1"}],
)  # fmt: skip
def test_a_namespace_that_is_not_this_runs_sandbox_is_refused_before_anything_is_created(
    tmp_path, labels
):
    guard, plan, runner = isolation(tmp_path, labels=labels)
    assert refused(guard, plan) == "REDTEAM_TARGET_NOT_ALLOWED"
    assert applied(runner) == []


def test_only_an_application_resource_is_a_valid_target(tmp_path):
    guard, plan, runner = isolation(tmp_path)
    for target in ("orders-db", "ghost"):
        assert refused(guard, plan, target) == "REDTEAM_TARGET_NOT_ALLOWED"
    assert applied(runner) == []


def test_a_second_session_is_refused_while_another_executor_exists(tmp_path):
    guard, plan, runner = isolation(tmp_path, running="cmp-sbx-someone-else/cmp-redteam-abc\n")
    assert refused(guard, plan) == "REDTEAM_CONCURRENCY_LIMIT"
    assert applied(runner) == []


def test_this_runs_own_leftover_after_a_crash_does_not_block_its_retry(tmp_path):
    from cmp_worker.sandbox_redteam import executor_name

    _, plan, _ = isolation(tmp_path)
    leftover = f"{plan.namespace}/{executor_name(plan.run_id)}\n"
    guard, plan, _ = isolation(tmp_path, running=leftover)
    with guard.session(plan, target_resource_id="orders-api") as executor:
        assert executor.name == executor_name(plan.run_id)


def test_everything_is_removed_when_the_work_inside_the_session_fails(tmp_path):
    guard, plan, runner = isolation(tmp_path)
    with (
        pytest.raises(RuntimeError),
        guard.session(plan, target_resource_id="orders-api") as executor,
    ):
        raise RuntimeError("boom")
    assert any(f"pod {executor.name}" in c for c in deleted(runner))
    assert any("networkpolicy cmp-redteam-egress" in c for c in deleted(runner))


def test_a_retry_replaces_leftovers_by_name_instead_of_adding_pods(tmp_path):
    guard, plan, runner = isolation(tmp_path)
    with guard.session(plan, target_resource_id="orders-api") as first:
        pass
    runner.calls.clear()
    with guard.session(plan, target_resource_id="orders-api") as second:
        names = [d["metadata"]["name"] for d in applied(runner) if d["kind"] == "Pod"]
    assert first.name == second.name and names.count(second.name) == 1
    calls = [" ".join(a) for a, _ in runner.calls]
    first_delete = next(i for i, c in enumerate(calls) if "delete pod" in c and second.name in c)
    first_apply = next(i for i, c in enumerate(calls) if " apply " in c)
    assert first_delete < first_apply  # leftovers of the same name go before anything is created


def test_no_secret_environment_or_token_reaches_the_cluster_calls(tmp_path):
    guard, plan, runner = isolation(tmp_path)
    with guard.session(plan, target_resource_id="orders-api"):
        pass
    sent = json.dumps(applied(runner))
    assert "generated-db-password" not in sent and "env-secret-value" not in sent
    for document in applied(runner):
        assert document["kind"] in {"Pod", "NetworkPolicy"}
        if document["kind"] == "Pod":
            assert restricted_violations(document) == []


# --- opt-in --------------------------------------------------------------------------------------


def test_red_team_is_off_by_default_and_needs_a_consent_reference_to_turn_on():
    assert SandboxSettings().redteam is False
    with pytest.raises(ValidationError):
        SandboxSettings(redteam=True)
    assert SandboxSettings(redteam=True, redteam_consent_ref="approval-1").redteam is True
