import json
import shutil
from datetime import UTC, datetime

import pytest
from cmp_contracts.provisioning import Image
from cmp_worker.sandbox_deploy import (
    SandboxCapabilities,
    SandboxError,
    SandboxImages,
    plan_update,
)

from .test_deploy import (
    APP,
    ENV_SECRET_VALUE,
    PG,
    FakeRunner,
    make_service,
    requirements,
    stdin_of,
)

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
NEW_APP = Image(
    uri="ghcr.io/example/demo-api@sha256:" + "c" * 64, platforms=["linux/amd64", "linux/arm64"]
)


def plan_with(
    service, tmp_path, *, app=APP, db=PG, runner_env=None, env_value=ENV_SECRET_VALUE, **kw
):
    env_file = tmp_path / ".env"
    env_file.write_text(f"FEATURE_X={env_value}\n")
    return service.prepare_sandbox(
        kw.pop("requirements", requirements()),
        migration_id="mig-1",
        run_id="run1",
        images=SandboxImages(database=db, applications={"orders-api": app}),
        capabilities=kw.pop("capabilities", SandboxCapabilities(storage_class="local-path")),
        env_names=["FEATURE_X"],
        env_file=env_file,
        now=datetime(2026, 11, 5, tzinfo=UTC),
        **kw,
    )


def pair(tmp_path, **changes):
    base = plan_with(make_service(FakeRunner()), tmp_path)
    return base, plan_with(make_service(FakeRunner()), tmp_path, **changes)


def test_an_image_only_change_updates_the_application_and_leaves_the_database(tmp_path):
    before, after = pair(tmp_path, app=NEW_APP)
    assert plan_update(before, after) == {"orders-db": "UNCHANGED", "orders-api": "UPGRADED"}


def test_a_database_image_change_inside_the_same_major_is_an_in_place_upgrade(tmp_path):
    other = Image(uri="docker.io/library/postgres@sha256:" + "d" * 64, platforms=PG.platforms)
    before, after = pair(tmp_path, db=other)
    assert plan_update(before, after) == {"orders-db": "UPGRADED", "orders-api": "UNCHANGED"}


def test_fields_a_statefulset_cannot_change_recreate_the_database_release(tmp_path):
    before, after = pair(tmp_path, capabilities=SandboxCapabilities(storage_class="other-class"))
    assert plan_update(before, after)["orders-db"] == "RECREATED"


def test_a_database_major_version_change_recreates_the_database_release(tmp_path):
    before, after = pair(tmp_path)
    changed = json.loads(after.model_dump_json())
    for resource in changed["request"]["resources"]:
        if resource["kind"] == "POSTGRESQL":
            resource["version"] = "17"
    assert plan_update(before, type(after).model_validate(changed))["orders-db"] == "RECREATED"


def test_unchanged_plans_change_nothing(tmp_path):
    before, after = pair(tmp_path)
    assert set(plan_update(before, after).values()) == {"UNCHANGED"}


def test_a_changed_env_secret_restarts_the_application_even_with_no_other_change(tmp_path):
    before = plan_with(make_service(FakeRunner()), tmp_path)
    env_name = f"{before.releases['orders-api']}-env"
    # The cluster still holds the old content hash, so the new values replace the Secret.
    runner = FakeRunner(
        existing={env_name: "x", f"{before.releases['orders-db']}-auth": "p"},
        outputs={"content-sha256": b"stale-hash"},
    )
    after = plan_with(
        make_service(runner), tmp_path, env_value="rotated-value-999", update_env=True
    )
    assert after.env_updated == [env_name]
    assert plan_update(before, after)["orders-api"] == "RESTARTED"
    replaced = [s for a, s in runner.calls if s is not None and "replace" in a]
    assert any(b"rotated-value-999" in s for s in replaced)
    assert "rotated-value-999" not in " ".join(" ".join(a) for a, _ in runner.calls)


def test_an_env_secret_with_the_same_content_is_left_alone(tmp_path):
    first = FakeRunner()
    before = plan_with(make_service(first), tmp_path)
    created = [json.loads(s) for s in stdin_of(first, "create")]
    env = next(s for s in created if s["metadata"]["name"].endswith("-env"))
    current_hash = env["metadata"]["annotations"]["cmp.sandbox/content-sha256"]
    runner = FakeRunner(
        existing={env["metadata"]["name"]: "x", f"{before.releases['orders-db']}-auth": "p"},
        outputs={"content-sha256": current_hash.encode()},
    )
    after = plan_with(make_service(runner), tmp_path, update_env=True)
    assert after.env_updated == [] and not [
        a for a, s in runner.calls if s and ("replace" in a or "create" in a)
    ]


def test_a_changed_set_of_resources_is_refused(tmp_path):
    before, after = pair(tmp_path)
    smaller = json.loads(after.model_dump_json())
    smaller["request"]["resources"] = smaller["request"]["resources"][:1]
    with pytest.raises(SandboxError) as exc:
        plan_update(before, type(after).model_validate(smaller))
    assert exc.value.code == "RESOURCE_SET_CHANGED"


@needs_helm
def test_an_image_only_update_runs_helm_for_the_application_alone_and_records_digests(tmp_path):
    runner = FakeRunner()
    service = make_service(runner)
    before = plan_with(service, tmp_path)
    service.apply_sandbox(before)
    after = plan_with(service, tmp_path, app=NEW_APP)
    runner.calls.clear()
    record = service.update_sandbox(before, after, attempt=2)
    installs = runner.commands("upgrade")
    assert len(installs) == 1 and after.releases["orders-api"] in installs[0]
    assert not runner.commands("uninstall")
    assert record.attempt == 2
    assert {k: v.action for k, v in record.resources.items()} == {
        "orders-db": "UNCHANGED",
        "orders-api": "UPGRADED",
    }
    assert record.resources["orders-api"].image_digest == "sha256:" + "c" * 64
    assert record.resources["orders-db"].image_digest == "sha256:" + "a" * 64
    assert all(len(v.values_sha256) == 64 for v in record.resources.values())


@needs_helm
def test_values_digests_are_stable_for_equal_input_and_change_with_the_image(tmp_path):
    service = make_service(FakeRunner())
    one = service.apply_sandbox(plan_with(service, tmp_path))["resources"]
    two = service.apply_sandbox(plan_with(service, tmp_path))["resources"]
    three = service.apply_sandbox(plan_with(service, tmp_path, app=NEW_APP))["resources"]
    assert one == two
    assert one["orders-api"]["values_sha256"] != three["orders-api"]["values_sha256"]
    assert one["orders-db"]["values_sha256"] == three["orders-db"]["values_sha256"]


@needs_helm
def test_a_recreated_database_is_uninstalled_and_its_volume_removed_before_installing(tmp_path):
    runner = FakeRunner()
    service = make_service(runner)
    before = plan_with(service, tmp_path)
    after = plan_with(
        service, tmp_path, capabilities=SandboxCapabilities(storage_class="other-class")
    )
    runner.calls.clear()
    record = service.update_sandbox(before, after, attempt=2)
    verbs = [
        a[a.index(v)]
        for a in (c for c, _ in runner.calls)
        for v in ("uninstall", "upgrade", "delete")
        if v in a
    ]
    assert verbs.index("uninstall") < verbs.index("delete") < verbs.index("upgrade")
    delete_pvc = next(a for a in runner.commands("delete") if "pvc" in a)
    assert before.releases["orders-db"] in delete_pvc
    assert record.resources["orders-db"].action == "RECREATED" and record.baseline == "LOST"
    assert any("restart" in a for a, _ in runner.calls)  # the app reconnects to the new database


@needs_helm
def test_a_restart_follows_an_env_secret_change(tmp_path):
    runner = FakeRunner()
    service = make_service(runner)
    before = plan_with(service, tmp_path)
    after = before.model_copy(update={"env_updated": [f"{before.releases['orders-api']}-env"]})
    runner.calls.clear()
    record = service.update_sandbox(before, after, attempt=3)
    restart = next(a for a, _ in runner.calls if "restart" in a)
    assert f"deployment/{before.releases['orders-api']}" in restart
    assert any("status" in a for a, _ in runner.calls)
    assert record.resources["orders-api"].action == "RESTARTED"


def test_the_baseline_is_dumped_inside_the_database_pod_without_a_password_on_the_command_line(
    tmp_path,
):
    runner = FakeRunner()
    service = make_service(runner)
    plan = plan_with(service, tmp_path)
    runner.calls.clear()
    assert service.snapshot_baseline(plan) == {"baseline": "SAVED"}
    command = " ".join(runner.commands("exec")[0])
    assert (
        "pg_dump" in command
        and "cmp-baseline.dump" in command
        and "generated-db-password" not in command
    )


def test_reset_restores_the_baseline_then_restarts_the_application(tmp_path):
    runner = FakeRunner()
    service = make_service(runner)
    plan = plan_with(service, tmp_path)
    runner.calls.clear()
    assert service.reset_test_data(plan) == {"reset": "OK"}
    text = [" ".join(a) for a, _ in runner.calls]
    assert any("DROP SCHEMA public CASCADE" in t and "pg_restore" in t for t in text)
    assert any("restart" in t and plan.releases["orders-api"] in t for t in text)


def test_reset_without_a_saved_baseline_fails_with_a_code(tmp_path):
    from cmp_worker.activities.k8s_workloads_cli import CommandError

    class NoBaseline(FakeRunner):
        def run(self, args, **kw):
            if "test -s" in " ".join(args):
                raise CommandError("kubectl")
            return super().run(args, **kw)

    service = make_service(NoBaseline())
    plan = plan_with(service, tmp_path)
    with pytest.raises(SandboxError) as exc:
        service.reset_test_data(plan)
    assert exc.value.code == "BASELINE_MISSING"


def test_the_database_can_be_cut_for_a_check_and_is_always_brought_back(tmp_path):
    runner = FakeRunner()
    service = make_service(runner)
    plan = plan_with(service, tmp_path)
    runner.calls.clear()
    with pytest.raises(RuntimeError), service.database_cut(plan):
        assert any("--replicas=0" in a for a, _ in runner.calls)
        raise RuntimeError("the check blew up")
    scale = [a for a, _ in runner.calls if "scale" in a]
    assert "--replicas=0" in scale[0] and "--replicas=1" in scale[1]
    assert f"statefulset/{plan.releases['orders-db']}" in scale[1]


def test_the_database_restart_waits_for_the_rollout(tmp_path):
    runner = FakeRunner()
    service = make_service(runner)
    plan = plan_with(service, tmp_path)
    runner.calls.clear()
    service.restart_database(plan)
    verbs = [a[a.index("rollout") + 1] for a, _ in runner.calls if "rollout" in a]
    assert verbs == ["restart", "status"]


def test_the_application_is_reached_through_the_service_proxy_path(tmp_path):
    service = make_service(FakeRunner())
    plan = plan_with(service, tmp_path)
    path = service.service_path(plan, "orders-api")
    assert path == (
        f"/api/v1/namespaces/{plan.namespace}/services/{plan.releases['orders-api']}:8080/proxy"
    )
