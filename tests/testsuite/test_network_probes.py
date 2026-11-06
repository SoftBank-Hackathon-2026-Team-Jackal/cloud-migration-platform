"""Ticket 30: network probes in the suite, run from a short-lived Pod and judged by the runner.

A probe step says who tries to connect (a Pod with the identity of an allowed source, or an
outsider), to which resource and port, and whether it should connect or be blocked. The runner
does not open connections: the caller supplies the implementation (the Sandbox worker uses a Pod).
Probe cases are made from the requirements' `network_intent` only, so they hold no name, port or
path of any one application.

Seam: the suite model and runner with fake probes, and the pure case generator.
"""

import gzip

import pytest
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_testsuite import Suite, SuiteCase, dump_bundle, load_bundle, run_case
from cmp_testsuite.bundle import sha256_of
from cmp_testsuite.model import ProbeStep
from cmp_testsuite.security import SKIPPED_TEMPLATES, security_probe_cases
from cmp_testsuite.standard import AppApi, standard_cases

# The SHA-256 of this standard suite's tar content from before probes existed. It is the content
# that is pinned, not the compressed bytes: gzip output depends on the zlib build, so the
# compressed SHA-256 of the same suite differs between a Mac and a Linux runner.
PINNED_TAR_SHA256 = "e87ca29323b771f3fd5794ed67d4a8d63973aa68dc6e3620a019d013bf692dd7"


def probe(role="OUTSIDER", expect="BLOCKED", *, source=None, target="db", port=5432):
    return ProbeStep(
        role=role, source_resource_id=source, target_resource_id=target, port=port, expect=expect
    )


def probe_case(*steps, case_id="sec-x"):
    return SuiteCase(
        id=case_id,
        source="STANDARD",
        effect="READ_ONLY",
        covers=["network-policy"],
        steps=list(steps),
    )


# --- the suite model ----------------------------------------------------------------------------


def test_a_suite_with_probes_round_trips_and_keeps_the_step_kinds():
    suite = Suite(name="s", cases=[probe_case(probe("ALLOWED_CLIENT", "CONNECTS", source="api"))])
    again = load_bundle(dump_bundle(suite))
    (step,) = again.cases[0].steps
    assert (
        isinstance(step, ProbeStep)
        and step.role == "ALLOWED_CLIENT"
        and step.source_resource_id == "api"
    )


def test_an_allowed_client_names_its_source_and_an_outsider_does_not():
    with pytest.raises(ValueError):
        probe("ALLOWED_CLIENT", "CONNECTS", source=None)
    with pytest.raises(ValueError):
        probe("OUTSIDER", "BLOCKED", source="api")


def test_a_probe_port_is_a_real_port():
    with pytest.raises(ValueError):
        probe(port=0)
    with pytest.raises(ValueError):
        probe(port=70000)


def test_suites_without_probes_keep_their_sha256():
    api = AppApi(
        health_path="/healthz", write_path="/items", read_path="/items/{{id}}",
        health_from_target=True,
    )  # fmt: skip
    suite = Suite(name="pin", cases=standard_cases(api))
    assert sha256_of(gzip.decompress(dump_bundle(suite))) == PINNED_TAR_SHA256


# --- the runner ---------------------------------------------------------------------------------


def test_a_probe_passes_when_the_answer_is_what_was_expected():
    seen = []

    def probes(step):
        seen.append((step.role, step.target_resource_id, step.port))
        return False  # blocked

    result = run_case(probe_case(probe()), "http://unused", probes=probes)
    assert result.status == "PASSED" and seen == [("OUTSIDER", "db", 5432)]


@pytest.mark.parametrize(
    ("expect", "connected", "reason"),
    [
        ("BLOCKED", True, "PROBE_MISMATCH expected=BLOCKED actual=CONNECTS"),
        ("CONNECTS", False, "PROBE_MISMATCH expected=CONNECTS actual=BLOCKED"),
    ],
)
def test_a_probe_fails_with_a_code_when_the_answer_differs(expect, connected, reason):
    role = "OUTSIDER" if expect == "BLOCKED" else "ALLOWED_CLIENT"
    source = None if expect == "BLOCKED" else "api"
    case = probe_case(probe(role, expect, source=source))
    result = run_case(case, "http://unused", probes=lambda step: connected)
    assert (result.status, result.reason) == ("FAILED", reason)


def test_without_a_probe_implementation_the_case_fails_instead_of_passing():
    result = run_case(probe_case(probe()), "http://unused")
    assert (result.status, result.reason) == ("FAILED", "PROBE_UNAVAILABLE")


def test_a_probe_implementation_that_breaks_is_an_unavailable_probe_not_a_crash():
    def broken(step):
        raise RuntimeError("cluster went away")

    result = run_case(probe_case(probe()), "http://unused", probes=broken)
    assert (result.status, result.reason) == ("FAILED", "PROBE_UNAVAILABLE")


# --- the cases come from the network intent ---------------------------------------------------


def requirements(*, apps=("api",), db=True, port=5432, intents=None):
    resources = [
        {
            "id": a, "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 100, "memory_mib": 128,
            "port": 8080, "health_path": "/healthz", "env_bindings": {},
        }
        for a in apps
    ]  # fmt: skip
    if db:
        resources.append(
            {
                "id": "db", "kind": "POSTGRESQL", "version": "16", "extensions": [],
                "database": {"name": "app", "username": "app"}, "cpu_millicores": 250,
                "memory_mib": 512, "storage_gib": 1,
            }
        )  # fmt: skip
    edges = (
        intents
        if intents is not None
        else [
            {"source_resource_id": a, "destination_resource_id": "db", "port": port} for a in apps
        ]
    )
    return RequirementsV2.model_validate(
        {"contract_version": "2", "resources": resources, "network_intent": edges}
    )


def steps_of(plan):
    return {
        c.id: [
            (s.role, s.source_resource_id, s.target_resource_id, s.port, s.expect) for s in c.steps
        ]
        for c in plan.cases
    }


def test_each_edge_to_the_database_is_probed_from_its_source_and_from_an_outsider():
    plan = security_probe_cases(requirements())
    assert steps_of(plan) == {
        "sec-db-allowed-api-db": [("ALLOWED_CLIENT", "api", "db", 5432, "CONNECTS")],
        "sec-db-outsider-db": [("OUTSIDER", None, "db", 5432, "BLOCKED")],
    }
    assert all(c.source == "STANDARD" and c.effect == "READ_ONLY" for c in plan.cases)
    assert all(c.covers == ["network-policy"] and not c.external_side_effects for c in plan.cases)


def test_the_port_and_the_sources_come_from_the_intent_and_nothing_else():
    plan = security_probe_cases(requirements(apps=("web", "worker"), port=6543))
    assert steps_of(plan) == {
        "sec-db-allowed-web-db": [("ALLOWED_CLIENT", "web", "db", 6543, "CONNECTS")],
        "sec-db-allowed-worker-db": [("ALLOWED_CLIENT", "worker", "db", 6543, "CONNECTS")],
        "sec-db-outsider-db": [("OUTSIDER", None, "db", 6543, "BLOCKED")],
    }


def test_resource_ids_become_valid_case_ids():
    plan = security_probe_cases(requirements(apps=("Orders_API",)))
    assert "sec-db-allowed-orders-api-db" in {c.id for c in plan.cases}


def test_edges_that_do_not_go_to_the_database_are_not_probed():
    external = {"source_resource_id": "api", "external_host": "api.pay.example.com", "port": 443}
    plan = security_probe_cases(requirements(intents=[external]))
    assert plan.cases == [] and ("DB_ACCESS", "NO_INTENT_TO_DATABASE") in plan.skipped


def test_without_a_database_nothing_is_probed_and_the_reason_is_kept():
    plan = security_probe_cases(requirements(db=False, intents=[]))
    assert plan.cases == [] and ("DB_ACCESS", "NO_DATABASE_IN_REQUIREMENTS") in plan.skipped


def test_the_templates_with_no_target_yet_are_listed_with_why():
    plan = security_probe_cases(requirements())
    assert set(SKIPPED_TEMPLATES) <= set(plan.skipped)
    assert dict(SKIPPED_TEMPLATES) == {
        "DB_SSL_REJECT": "TARGET_DB_SSL_NOT_CONFIGURED",
        "ADMIN_PATH_BLOCKED": "ADMIN_PATH_NOT_CONFIRMED",
    }


def test_the_same_requirements_give_the_same_cases_in_the_same_order():
    first = security_probe_cases(requirements(apps=("web", "worker")))
    assert [c.id for c in first.cases] == [
        c.id for c in security_probe_cases(requirements(apps=("worker", "web"))).cases
    ]
    assert dump_bundle(Suite(name="x", cases=first.cases)) == dump_bundle(
        Suite(name="x", cases=security_probe_cases(requirements(apps=("worker", "web"))).cases)
    )
