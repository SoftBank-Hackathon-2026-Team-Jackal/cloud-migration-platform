"""Ticket 26: the Sandbox pulls private platform ECR images with a short-lived pull Secret."""

import json
from datetime import UTC, datetime

import pytest
from cmp_providers.ecr_pull import EcrPullError, EcrTokenMinter, PlatformRegistry
from cmp_worker.sandbox_deploy import SandboxCapabilities, SandboxError, SandboxImages

from .test_deploy import PG, FakeRunner, make_service, requirements, stdin_of

HOST = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
ECR_IMAGE = f"{HOST}/cmp-platform-builds@sha256:" + "d" * 64
TOKEN = "ecr-token-that-must-not-leak"


class FakeEcr:
    def __init__(self, endpoint=f"https://{HOST}", fail=False):
        self.endpoint, self.fail, self.calls = endpoint, fail, 0

    def get_authorization_token(self):
        import base64

        self.calls += 1
        if self.fail:
            raise RuntimeError("AccessDenied: arn:aws:iam::123456789012:role/x")
        return {
            "authorizationData": [
                {
                    "authorizationToken": base64.b64encode(f"AWS:{TOKEN}".encode()).decode(),
                    "proxyEndpoint": self.endpoint,
                    "expiresAt": datetime(2026, 11, 7, tzinfo=UTC),
                }
            ]
        }


def registry():
    return PlatformRegistry.parse(HOST, "cmp-platform-builds")


def minter(**kw):
    ecr = FakeEcr(**kw)
    return EcrTokenMinter(lambda region: ecr), ecr


def plan_with(image_uri):
    from cmp_contracts.provisioning import Image

    from .test_deploy import APP

    app = Image(uri=image_uri, platforms=["linux/amd64"]) if image_uri else APP
    return make_service(FakeRunner()).prepare_sandbox(
        requirements(),
        migration_id="mig-1",
        run_id="run1",
        images=SandboxImages(database=PG, applications={"orders-api": app}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=[],
        env_file=None,
        now=datetime(2026, 11, 5, tzinfo=UTC),
    )


def test_public_images_need_no_secret_and_no_token():
    plan = plan_with(None)
    runner = FakeRunner()
    mint, ecr = minter()
    assert make_service(runner).apply_pull_secret(plan, registry(), mint) is None
    assert runner.calls == [] and ecr.calls == 0


def test_a_platform_ecr_image_gets_a_pull_secret_and_the_default_account_uses_it():
    plan = plan_with(ECR_IMAGE)
    runner = FakeRunner()
    mint, _ = minter()
    name = make_service(runner).apply_pull_secret(plan, registry(), mint)
    assert name == "cmp-ecr-pull"
    secret = next(json.loads(s) for s in stdin_of(runner, "apply") if b'"Secret"' in s)
    assert secret["type"] == "kubernetes.io/dockerconfigjson"
    assert secret["metadata"]["namespace"] == plan.namespace
    config = json.loads(secret["stringData"][".dockerconfigjson"])
    assert HOST in config["auths"] and config["auths"][HOST]["username"] == "AWS"
    account = next(json.loads(s) for s in stdin_of(runner, "apply") if b'"ServiceAccount"' in s)
    assert account["metadata"] == {"name": "default", "namespace": plan.namespace}
    assert account["imagePullSecrets"] == [{"name": "cmp-ecr-pull"}]


def test_the_token_never_appears_in_a_command_argument():
    plan = plan_with(ECR_IMAGE)
    runner = FakeRunner()
    mint, _ = minter()
    make_service(runner).apply_pull_secret(plan, registry(), mint)
    assert TOKEN not in " ".join(" ".join(args) for args, _ in runner.calls)


def test_an_image_outside_the_platform_registry_is_refused_before_a_token_is_asked():
    other = "999999999999.dkr.ecr.ap-northeast-2.amazonaws.com/other@sha256:" + "d" * 64
    plan = plan_with(other)
    mint, ecr = minter()
    with pytest.raises(SandboxError, match="IMAGE_REGISTRY_NOT_ALLOWED"):
        make_service(FakeRunner()).apply_pull_secret(plan, registry(), mint)
    assert ecr.calls == 0


def test_an_ecr_image_without_a_platform_registry_is_refused():
    plan = plan_with(ECR_IMAGE)
    with pytest.raises(SandboxError, match="IMAGE_REGISTRY_NOT_ALLOWED"):
        make_service(FakeRunner()).apply_pull_secret(plan, None, None)


def test_a_token_failure_is_a_code_with_no_aws_message():
    plan = plan_with(ECR_IMAGE)
    mint, _ = minter(fail=True)
    with pytest.raises(SandboxError) as exc:
        make_service(FakeRunner()).apply_pull_secret(plan, registry(), mint)
    assert str(exc.value) == "ECR_TOKEN_UNAVAILABLE" and "arn:" not in str(exc.value)
    mismatch, _ = minter(endpoint="https://999999999999.dkr.ecr.ap-northeast-2.amazonaws.com")
    with pytest.raises(SandboxError, match="ECR_ACCOUNT_MISMATCH"):
        make_service(FakeRunner()).apply_pull_secret(plan, registry(), mismatch)


def test_the_same_call_again_replaces_the_secret_so_an_update_run_stays_fresh():
    plan = plan_with(ECR_IMAGE)
    runner = FakeRunner()
    mint, ecr = minter()
    service = make_service(runner)
    service.apply_pull_secret(plan, registry(), mint)
    service.apply_pull_secret(plan, registry(), mint)
    assert ecr.calls == 2 and len(stdin_of(runner, "apply")) == 4


def test_the_error_type_is_the_providers_so_codes_stay_the_same():
    assert issubclass(EcrPullError, ValueError)


# ------------------------------------------------------------ Activity and worker wiring


@pytest.mark.asyncio
async def test_the_apply_activity_asks_for_the_pull_secret_before_installing(tmp_path):
    from cmp_worker.activities.sandbox_loop import SandboxInput, SandboxLoopActivities
    from cmp_worker.artifact_store import LocalArtifactStore

    order = []

    class Service:
        cluster = None

        def database_cut(self, plan):
            raise AssertionError("not used")

        def apply_pull_secret(self, plan, reg, mint):
            order.append(("pull", reg, mint))

        def apply_sandbox(self, plan):
            order.append("apply")

        def apply_network(self, plan, hosts):
            order.append("network")

        def check_sandbox(self, plan):
            order.append("check")

        def snapshot_baseline(self, plan):
            order.append("baseline")

    store = LocalArtifactStore(tmp_path)
    ref = store.put(b"{}", ref_id="x", kind="k")
    data = SandboxInput(
        migration_id="m", run_id="r", requirements_ref=ref, suite_ref=ref,
        source={"workspace": "/ws"}, database_image=PG,
    )  # fmt: skip
    mint, _ = minter()
    activities = SandboxLoopActivities(
        service=Service(), store=store, workspace_root=tmp_path / "w", build=lambda *_: None,
        suite_runner=lambda *_: None, pull_registry=registry(), token_minter=mint,
    )  # fmt: skip
    plan = plan_with(None)
    out = await activities.sbx_apply(
        {
            "input": data.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "previous_plan": None,
        }
    )
    assert out["sandbox_error"] is None
    assert (
        order[0][0] == "pull"
        and order[0][1] is not None
        and order[1:] == ["apply", "network", "check", "baseline"]
    )


def test_the_worker_gets_the_platform_registry_from_the_build_settings(tmp_path):
    from cmp_worker.workers.sandbox import build_sandbox_loop

    from .test_worker_wiring import FULL_BUILD, config

    loop = build_sandbox_loop(config(tmp_path, **FULL_BUILD))
    assert loop.pull_registry.registry == HOST and loop.pull_registry.repositories == {"shop"}
    assert loop.token_minter is not None
    plain = build_sandbox_loop(config(tmp_path))
    assert plain.pull_registry is None and plain.token_minter is None
