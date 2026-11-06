"""Provisioning 63: the app's own health path travels analysis -> Plan -> result -> Traffic handoff.

Seams:
- Analysis: real `scan_manifests` / `scan_go_routes` / `build_requirements` on a workspace shaped
  like the demo chat-app (Deployment readinessProbe `/healthz`, `GET /healthz` in main.go).
- Plan: real `prepare_plan_v2` + `deployment_request` (the 04B request every provider renders).
- Handoff: real `build_provisioning_result` -> `handoff` -> `traffic_handover` for AWS, GCP and
  ON_PREM refs (the refs key `app_health_path` is what the provider executions write).
Misses: a real cluster, Agent and relay.
"""

import json

import pytest
from cmp_providers.provisioning_result import (
    DEFAULT_HEALTH_PATH,
    build_provisioning_result,
    handoff,
    traffic_handover,
)
from cmp_scanners.assembly import build_requirements
from cmp_scanners.code import scan_code
from cmp_scanners.manifests import scan_manifests
from cmp_scanners.routes import scan_go_routes
from cmp_worker.plan_publish import deployment_request
from cmp_worker.provisioning_v2 import prepare_plan_v2
from test_provisioning_result import (
    AWS_REFS,
    GCP_WORKLOAD_REFS,
    ONPREM_REFS,
    plan_manifest,
)
from test_provisioning_v2 import _request, _requirements
from test_traffic_handoff import FINISHED, TUNNEL_OK

PROBE = (
    "        readinessProbe:\n"
    "          httpGet:\n"
    "            path: {path}\n"
    "            port: http\n"
)
DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {{name: chat-app}}\nspec:\n"
    "  template:\n    spec:\n      containers:\n"
    "      - name: chat-app\n        image: 'chat-app:1'\n"
    "        ports: [{{containerPort: 8080, name: http}}]\n"
    "{probe}"
)
MAIN_GO = (
    "package main\n\nfunc main() {\n"
    '\tmux.HandleFunc("GET /healthz", s.healthz)\n'
    '\tmux.HandleFunc("POST /api/messages", s.postMessage)\n'
    '\tmux.Handle("GET /", http.FileServerFS(static))\n'
    "}\n"
)


def workspace(tmp_path, *, probe: str | None, main_go: str | None = MAIN_GO):
    (tmp_path / "k8s").mkdir()
    rendered = PROBE.format(path=probe) if probe else ""
    (tmp_path / "k8s" / "chat-app-deployment.yaml").write_text(DEPLOYMENT.format(probe=rendered))
    if main_go is not None:
        (tmp_path / "main.go").write_text(main_go)
    return tmp_path


def app_requirement(root):
    built = build_requirements(
        scan_manifests(root), scan_code(root, {}, []), {}, scan_go_routes(root)
    )
    [app] = [r for r in built.requirements.resources if r.kind == "APPLICATION"]
    return app, built.assumptions


# ---- analysis: readiness probe > liveness probe > health-like code route > default -------------


def test_the_demo_app_probe_path_becomes_the_requirement(tmp_path):
    app, assumptions = app_requirement(workspace(tmp_path, probe="/healthz"))
    assert app.health_path == "/healthz"
    assert not any("HEALTH_PATH" in a for a in assumptions)


def test_the_probe_wins_over_a_code_route(tmp_path):
    app, _ = app_requirement(workspace(tmp_path, probe="/ready-check"))
    assert app.health_path == "/ready-check"


def test_without_a_probe_a_health_like_code_route_is_used_and_recorded(tmp_path):
    app, assumptions = app_requirement(workspace(tmp_path, probe=None))
    assert app.health_path == "/healthz"
    assert any(
        a.startswith("HEALTH_PATH_FROM_CODE_ROUTE:") and "main.go:4" in a for a in assumptions
    )
    assert not any("HEALTH_PATH_NOT_DECLARED" in a for a in assumptions)


def test_a_post_only_health_route_does_not_count(tmp_path):
    post_only = 'package main\n\nfunc main() {\n\tmux.HandleFunc("POST /healthz", h)\n}\n'
    app, assumptions = app_requirement(workspace(tmp_path, probe=None, main_go=post_only))
    assert app.health_path == "/"
    assert any("HEALTH_PATH_NOT_DECLARED" in a for a in assumptions)


def test_without_probe_or_route_the_default_is_kept(tmp_path):
    app, assumptions = app_requirement(workspace(tmp_path, probe=None, main_go=None))
    assert app.health_path == "/"
    assert any("HEALTH_PATH_NOT_DECLARED" in a for a in assumptions)


# ---- Plan: the requirement's path is the one every provider renders ---------------------------


def test_the_plan_request_carries_the_analysed_health_path():
    requirements = _requirements()
    requirements["resources"][1]["health_path"] = "/healthz"
    payload, content = _request(requirements)
    spec = prepare_plan_v2(payload, requirements_bytes=content).spec

    class Target:
        namespace = "demo"
        storage_class = "local-path"
        database_image = "docker.io/library/postgres@sha256:" + "c" * 64

    request = deployment_request(spec, Target())
    [app] = [r for r in request["resources"] if r["kind"] == "APPLICATION"]
    assert app["health_path"] == "/healthz"
    assert json.loads(content)["resources"][1]["health_path"] == "/healthz"


# ---- handoff: AWS, GCP and ON_PREM all forward the same path ----------------------------------


def traffic(provider, scope, refs, tunnel=None):
    result = build_provisioning_result(
        plan_manifest(provider, scope),
        approval_id="approval-1",
        status="SUCCEEDED",
        code="WORKLOADS_READY",
        step=None,
        refs=refs,
        finished_at=FINISHED,
    )
    return traffic_handover(handoff(result, "TRAFFIC_MIGRATION", "plan-001", 1, tunnel))


CASES = [
    ("AWS", "WORKLOADS", AWS_REFS, None),
    ("GCP", "WORKLOADS", GCP_WORKLOAD_REFS, None),
    ("ON_PREM", "FULL", ONPREM_REFS, TUNNEL_OK),
]


@pytest.mark.parametrize(("provider", "scope", "refs", "tunnel"), CASES)
def test_every_provider_hands_the_plan_health_path_to_traffic(provider, scope, refs, tunnel):
    converted = traffic(provider, scope, {**refs, "app_health_path": "/healthz"}, tunnel)
    assert converted.proceed, converted.blocked_by
    assert converted.handover.health_path == "/healthz"


@pytest.mark.parametrize(("provider", "scope", "refs", "tunnel"), CASES)
def test_a_result_without_a_health_path_falls_back_to_the_default(provider, scope, refs, tunnel):
    without = {k: v for k, v in refs.items() if k != "app_health_path"}
    converted = traffic(provider, scope, without, tunnel)
    assert converted.handover.health_path == DEFAULT_HEALTH_PATH == "/health"
