"""Ticket 38: from the run's settings to the Worker's kit."""

import base64
import json

import pytest
from cmp_agents.llm import FakeLLMClient
from cmp_worker.redteam_wiring import build_redteam_kit, sandbox_secret_values
from cmp_worker.sandbox_input import SandboxSettings
from cmp_worker.sandbox_isolation import Executor

from .test_redteam_safeguards import sandbox_plan
from .test_redteam_tools import Cluster

ROUTES = [{"method": "GET", "path": "/items", "params": ["id"]}]


def enc(text):
    return base64.b64encode(text.encode()).decode()


def test_switching_the_red_team_on_needs_consent_at_settings_time():
    ok = SandboxSettings(redteam=True, redteam_consent_ref="c-1", redteam_endpoints=ROUTES)
    assert ok.redteam_intensity.value == "PASSIVE" and ok.redteam_time_budget_seconds == 300
    with pytest.raises(ValueError, match="REDTEAM_CONSENT_MISSING"):
        SandboxSettings(redteam=True, redteam_endpoints=ROUTES)
    # A route list is no longer required here: a Go app's own scanned routes can fill it in at
    # assemble time (`assemble_sandbox_input`, see tests/analysis/test_sandbox_input.py). Only if
    # neither source has one does assembling the input itself raise REDTEAM_ENDPOINTS_MISSING.
    SandboxSettings(redteam=True, redteam_consent_ref="c-1")
    SandboxSettings()  # off by default, and nothing else is asked for


def test_a_route_in_the_settings_cannot_name_a_host():
    for bad in ("http://other/x", "//other/x", "/10.0.0.9/x"):
        with pytest.raises(ValueError):
            SandboxSettings(
                redteam=True, redteam_consent_ref="c-1",
                redteam_endpoints=[{"method": "GET", "path": bad}],
            )  # fmt: skip


def test_the_secret_values_of_the_sandbox_are_collected_for_masking_and_stay_in_memory(tmp_path):
    plan = sandbox_plan(tmp_path)
    secrets = {
        "items": [
            {"type": "Opaque", "data": {"DB_PASSWORD": enc("Zx9-live-key-4471"), "A": enc("ab")}},
            {
                "type": "kubernetes.io/service-account-token",
                "data": {"token": enc("sa-token-value")},
            },
            {"type": "Opaque", "data": {"BIN": base64.b64encode(b"\xff\xfe\x00").decode()}},
        ]
    }
    cluster = Cluster(json.dumps(secrets).encode())
    found = sandbox_secret_values(cluster, plan)
    assert found == ["Zx9-live-key-4471"]  # too short, binary and service-account tokens are out
    assert Cluster(b"not json") and sandbox_secret_values(Cluster(b"not json"), plan) == []


def test_the_kit_uses_the_workers_cluster_and_books_the_model_to_the_migration(tmp_path):
    asked = []

    def factory(migration_id, role):
        asked.append((migration_id, role))
        return FakeLLMClient([])

    class Service:
        cluster = Cluster()

    kit = build_redteam_kit(Service(), object(), factory)
    kit.brain("mig-7")
    assert asked == [("mig-7", "redteam")]
    assert set(kit.allowed._names) == {"probe_get"}
    executor = Executor(name="x", namespace="ns", target_ip="10.0.0.1", port=80)
    kit.dispatch(executor, ["s3cretvalue"])  # builds, runs nothing
    assert Service.cluster.calls == []
