import json

import pytest
from cmp_worker.config import Settings
from cmp_worker.preflight import FAIL, OK, WARN, ok, run_checks, summary
from cmp_worker.start_input import build_start_input
from pydantic import ValidationError

DIGEST = "sha256:" + "a" * 64


def settings(tmp_path, **kw):
    kube = tmp_path / "kube.yaml"
    kube.write_text("x")
    base = {
        "analysis_artifact_store_dir": str(tmp_path / "store"),
        "analysis_workspace_root": str(tmp_path / "ws"),
        "sandbox_kubeconfig": str(kube),
        "sandbox_prebuilt_images": json.dumps({"api": f"ghcr.io/x/api@{DIGEST}"}),
    }
    return Settings(_env_file=None, **{**base, **kw})


def fine(command):
    return (0, "v3.17.3" if command[0] == "helm" else "ok")


def check(config, **kw):
    kw.setdefault("run", fine)
    kw.setdefault("which", lambda tool: f"/usr/bin/{tool}")
    kw.setdefault("connect", lambda host, port: True)
    kw.setdefault("environ", {})
    return {c.name: c for c in run_checks(config, **kw)}


def test_a_complete_setup_passes_and_only_warns_about_the_model(tmp_path):
    result = check(settings(tmp_path))
    assert ok(list(result.values())) and result["llm"].status == WARN
    assert result["sandbox:cluster"].status == OK and result["helm:version"].status == OK


def test_missing_tools_an_unreachable_temporal_and_an_unwritable_store_fail(tmp_path):
    result = check(
        settings(tmp_path, analysis_artifact_store_dir=None),
        which=lambda tool: None if tool == "helm" else "/x",
        connect=lambda *_: False,
    )
    assert result["tool:helm"].status == FAIL and result["temporal"].status == FAIL
    assert result["analysis:store"].status == FAIL and not ok(list(result.values()))


def test_helm_4_is_refused_because_the_charts_are_tested_with_helm_3(tmp_path):
    result = check(settings(tmp_path), run=lambda c: (0, "v4.3.1") if c[0] == "helm" else (0, ""))
    assert result["helm:version"].status == FAIL


def test_a_cluster_that_cannot_be_reached_fails(tmp_path):
    result = check(settings(tmp_path), run=lambda c: (1, "") if c[0] == "kubectl" else (0, "v3.1"))
    assert result["sandbox:cluster"].status == FAIL


def test_a_missing_kubeconfig_and_unpinned_images_are_reported(tmp_path):
    assert (
        check(settings(tmp_path, sandbox_kubeconfig=str(tmp_path / "none")))[
            "sandbox:kubeconfig"
        ].status
        == FAIL
    )
    bad = check(settings(tmp_path, sandbox_prebuilt_images=json.dumps({"api": "x/y:latest"})))
    assert bad["sandbox:images"].status == FAIL
    unset = check(settings(tmp_path, sandbox_prebuilt_images=None))
    assert unset["sandbox:images"].status == WARN


def test_the_model_needs_a_key_a_ledger_and_no_stop_switch_and_the_key_is_never_shown(tmp_path):
    on = settings(tmp_path, llm_enabled=True)
    assert check(on)["llm"].status == FAIL
    ready = settings(tmp_path, llm_enabled=True, llm_ledger_path=str(tmp_path / "led" / "l.json"))
    result = check(ready, environ={"ANTHROPIC_API_KEY": "sk-ant-secret-value"})
    assert result["llm"].status == OK and "$3/run" in result["llm"].detail
    assert "sk-ant-secret-value" not in summary(list(result.values()))
    stop = tmp_path / "STOP"
    stop.write_text("")
    stopped = settings(
        tmp_path,
        llm_enabled=True,
        llm_ledger_path=str(tmp_path / "l.json"),
        llm_kill_switch_path=str(stop),
    )
    assert "kill switch" in check(stopped, environ={"ANTHROPIC_API_KEY": "k"})["llm"].detail


def test_planning_must_read_the_same_store_the_analysis_writes(tmp_path):
    config = settings(tmp_path)
    assert (
        check(config, api_store_dir=str(tmp_path / "other"))["store:shared-with-planning"].status
        == WARN
    )
    assert (
        check(config, api_store_dir=str(tmp_path / "store"))["store:shared-with-planning"].status
        == OK
    )


def test_a_local_repository_needs_the_development_flag(tmp_path):
    repo = tmp_path / "app"
    (repo / ".git").mkdir(parents=True)
    refused = check(settings(tmp_path), repository=str(repo))
    assert refused["source:local-clone"].status == FAIL
    allowed = check(
        settings(tmp_path, analysis_allow_local_repositories=True), repository=str(repo)
    )
    assert "source:local-clone" not in allowed and allowed["source:repository"].status == OK


# --------------------------------------------------------------- start input


def test_the_start_input_is_what_the_parent_workflow_expects(tmp_path):
    data = build_start_input(
        project_id="demo-1", repository="/src/demo-app", commit="a" * 40, manifests_dir="k8s",
        sandbox_json='{"write_path": "/api/messages", "read_path": "/api/messages/{{id}}"}',
    )  # fmt: skip
    assert data["project_id"] == "demo-1" and data["analysis"]["requested_commit"] == "a" * 40
    assert (
        data["analysis"]["repository"] == "/src/demo-app"
        and data["analysis"]["manifests_dir"] == "k8s"
    )
    assert data["sandbox"] == {"write_path": "/api/messages", "read_path": "/api/messages/{{id}}"}
    assert "credential" not in json.dumps(data).lower().replace("credential_ref", "")


def test_sandbox_settings_are_optional_and_typos_are_refused():
    assert build_start_input(project_id="p", repository="r", commit="c")["sandbox"] is None
    with pytest.raises(ValidationError):
        build_start_input(
            project_id="p", repository="r", commit="c", sandbox_json='{"strage_class": "x"}'
        )
    with pytest.raises(ValueError):
        build_start_input(project_id="p", repository="r", commit="c", sandbox_json="not json")


def test_the_source_token_setting_is_checked_without_showing_the_token(tmp_path):
    none = check(settings(tmp_path))
    assert none["source:token"].status == OK and "public" in none["source:token"].detail
    missing = check(settings(tmp_path, source_token_resolver="env"))
    assert missing["source:token"].status == FAIL
    present = check(
        settings(tmp_path, source_token_resolver="env"),
        environ={"CMP_GITHUB_TOKEN": "ghp_secretvalue123"},
    )
    assert present["source:token"].status == OK and "ghp_secretvalue123" not in summary(
        list(present.values())
    )
    assert (
        check(settings(tmp_path, source_token_resolver="platform"))["source:token"].status == FAIL
    )


def test_start_input_carries_the_dev_reference_and_never_a_token():
    data = build_start_input(
        project_id="p",
        repository="https://github.com/acme/demo-app",
        commit="a" * 40,
        source_access="dev-env",
    )
    assert data["analysis"]["source_access_ref"] == {"id": "dev-env", "version": "1"}
    assert (
        build_start_input(project_id="p", repository="r", commit="c")["analysis"][
            "source_access_ref"
        ]
        is None
    )


def test_build_settings_are_off_complete_or_refused(tmp_path):
    assert check(settings(tmp_path))["build"].status == WARN
    full = {
        "build_source_bucket": "cmp-sources",
        "build_ecr_repository": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/shop",
        "build_log_bucket": "cmp-build-logs",
        "build_platform": "linux/amd64",
    }
    assert check(settings(tmp_path, **full))["build"].status == OK
    assert check(settings(tmp_path, build_source_bucket="cmp-sources"))["build"].status == FAIL
