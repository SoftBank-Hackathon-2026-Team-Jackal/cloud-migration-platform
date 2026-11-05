import json

import pytest
from cmp_worker.config import Settings
from cmp_worker.prebuilt_builder import BuildUnavailable, PrebuiltImages
from cmp_worker.sandbox_deploy import SANDBOX_CONTEXT
from cmp_worker.workers.sandbox import build_sandbox_loop
from temporalio.exceptions import ApplicationError

DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/example/orders-api@{DIGEST}"


def config(tmp_path, **kw):
    kubeconfig = tmp_path / "kube.yaml"
    kubeconfig.write_text("apiVersion: v1\n")
    base = {
        "sandbox_kubeconfig": str(kubeconfig),
        "analysis_artifact_store_dir": str(tmp_path / "store"),
        "analysis_workspace_root": str(tmp_path / "ws"),
    }
    return Settings(_env_file=None, **{**base, **kw})


def test_without_a_kubeconfig_or_a_store_only_the_scaffold_runs(tmp_path):
    assert build_sandbox_loop(Settings(_env_file=None)) is None
    assert build_sandbox_loop(config(tmp_path, sandbox_kubeconfig=None)) is None
    assert build_sandbox_loop(config(tmp_path, analysis_artifact_store_dir=None)) is None


def test_a_missing_kubeconfig_file_stops_the_worker_at_start_up(tmp_path):
    with pytest.raises(ValueError, match="SANDBOX_KUBECONFIG_NOT_FOUND"):
        build_sandbox_loop(config(tmp_path, sandbox_kubeconfig=str(tmp_path / "nope.yaml")))


def test_the_loop_uses_the_dedicated_cluster_the_shared_store_and_the_configured_images(tmp_path):
    loop = build_sandbox_loop(
        config(
            tmp_path,
            sandbox_prebuilt_images=json.dumps({"orders-api": IMAGE}),
            sandbox_helm_timeout="90s",
        )
    )
    assert loop is not None
    assert loop.service.cluster.context == SANDBOX_CONTEXT and loop.service.helm_timeout == "90s"
    assert loop.store.root == tmp_path / "store"
    assert loop.workspace_root == tmp_path / "ws" / "sandbox-fixes"
    assert loop.build("orders-api", {}).uri == IMAGE
    assert (loop.service.database_chart_dir / "Chart.yaml").is_file()
    assert (loop.service.application_chart_dir / "Chart.yaml").is_file()
    assert loop.llm_factory is None  # the model stays off unless it is switched on


def test_the_model_is_wired_in_only_when_switched_on_with_a_key_and_a_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    loop = build_sandbox_loop(
        config(tmp_path, llm_enabled=True, llm_ledger_path=str(tmp_path / "ledger.json"))
    )
    assert loop.llm_factory is not None
    assert loop.llm_factory("mig-1", "fixer").run_id == "mig-1"


def test_images_that_are_not_pinned_by_digest_stop_the_worker_at_start_up(tmp_path):
    with pytest.raises(BuildUnavailable) as exc:
        build_sandbox_loop(
            config(
                tmp_path, sandbox_prebuilt_images=json.dumps({"orders-api": "ghcr.io/x/y:latest"})
            )
        )
    assert exc.value.code == "PREBUILT_IMAGE_NOT_DIGEST_PINNED"


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"a": 1}'])
def test_malformed_image_settings_are_refused(text):
    with pytest.raises(BuildUnavailable) as exc:
        PrebuiltImages.from_json(text)
    assert exc.value.code == "PREBUILT_IMAGES_INVALID"


def test_an_unconfigured_or_unknown_image_has_its_own_code():
    with pytest.raises(BuildUnavailable) as none:
        PrebuiltImages.from_json(None)("orders-api", {})
    assert none.value.code == "BUILD_NOT_CONFIGURED"
    with pytest.raises(BuildUnavailable) as unknown:
        PrebuiltImages({"orders-api": IMAGE})("billing", {})
    assert unknown.value.code == "PREBUILT_IMAGE_MISSING"


@pytest.mark.asyncio
async def test_a_missing_image_fails_the_step_without_retries_and_names_the_reason(tmp_path):
    from cmp_contracts.provisioning_v2 import RequirementsV2  # noqa: F401
    from cmp_contracts.verification import canonical_requirements
    from cmp_worker.activities.sandbox_loop import SandboxLoopActivities
    from cmp_worker.artifact_store import LocalArtifactStore

    from .test_deploy import FakeRunner, make_service, requirements

    store = LocalArtifactStore(tmp_path / "store")
    ref = store.put(
        canonical_requirements(requirements())[0], ref_id="requirements", kind="requirements"
    )
    activities = SandboxLoopActivities(
        service=make_service(FakeRunner()), store=store, workspace_root=tmp_path / "w",
        build=PrebuiltImages.from_json(None), suite_runner=lambda *_: None,
    )  # fmt: skip
    with pytest.raises(ApplicationError) as exc:
        await activities.sbx_build({"requirements_ref": ref.model_dump(), "source": {}})
    assert exc.value.type == "BUILD_NOT_CONFIGURED" and exc.value.non_retryable


FULL_BUILD = {
    "build_source_bucket": "cmp-sources",
    "build_ecr_repository": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/shop",
    "build_log_bucket": "cmp-build-logs",
    "build_platform": "linux/amd64",
}


def test_complete_build_settings_switch_the_real_build_path_on(tmp_path):
    from cmp_worker.build_wiring import ImageBuildAdapter

    loop = build_sandbox_loop(config(tmp_path, build_aws_profile="build", **FULL_BUILD))
    assert isinstance(loop.build, ImageBuildAdapter)
    assert loop.build.settings.aws_profile == "build"


def test_partial_build_settings_stop_the_worker_at_start_up(tmp_path):
    with pytest.raises(ValueError, match="BUILD_SETTINGS_INCOMPLETE"):
        build_sandbox_loop(config(tmp_path, build_source_bucket="cmp-sources"))


def test_without_build_settings_the_prebuilt_images_stay(tmp_path):
    loop = build_sandbox_loop(config(tmp_path))
    assert isinstance(loop.build, PrebuiltImages)
