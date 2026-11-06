"""ENV-06: the Sandbox builds the app's env from `env_file_ref` + EnvPlan with `render_env`."""

import json
import shutil
from datetime import UTC, datetime

import pytest
from cmp_contracts.env_plan import EnvPlan, EnvPlanEntry
from cmp_providers.env_file import InMemoryEnvFileStore
from cmp_worker.sandbox_deploy import SandboxCapabilities, SandboxError, SandboxImages

from .test_deploy import (
    APP,
    ENV_SECRET_VALUE,
    PG,
    ROOT,
    FakeRunner,
    make_service,
    requirements,
    stdin_of,
)

VALUE = "feature-value-that-must-not-leak"


def entries(**extra):
    base = {
        "DB_HOST": ("REPLACED", "host"),
        "DB_PASSWORD": ("REPLACED", "password_secret_ref"),
        "DATABASE_URL": ("REPLACED", "connection_url_secret_ref"),
        "FEATURE_X": ("PASS", None),
        "AWS_REGION": ("EXCLUDED", None),
        **extra,
    }
    return [
        EnvPlanEntry(key=k, disposition=d, binding=b, reason="DB_OUTPUT_BINDING" if b else None)
        for k, (d, b) in base.items()
    ]


def world(tmp_path, plan_entries=None, *, text=None):
    store = InMemoryEnvFileStore()
    ref = store.put((text or f"FEATURE_X={VALUE}\nOTHER=never-copied\nDB_HOST=old\n").encode())
    plan = EnvPlan(env_file_ref=ref, entries=plan_entries or entries())
    return store, ref, plan


def prepare(runner, store, ref, plan, **kw):
    return make_service(runner).prepare_sandbox(
        requirements(True),
        migration_id="mig-1",
        run_id="run1",
        images=SandboxImages(database=PG, applications={"orders-api": APP}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=[],
        env_file=None,
        now=datetime(2026, 11, 5, tzinfo=UTC),
        env_plan=plan,
        env_file_ref=ref,
        env_store=store,
        **kw,
    )


def created(runner):
    return {
        json.loads(s)["metadata"]["name"]: json.loads(s)["stringData"]
        for s in stdin_of(runner, "create")
    }


def test_pass_keys_go_to_the_app_secret_and_db_keys_to_the_connection_secret(tmp_path):
    store, ref, plan = world(tmp_path)
    runner = FakeRunner()
    result = prepare(runner, store, ref, plan)
    secrets = created(runner)
    app, db = result.releases["orders-api"], result.releases["orders-db"]
    assert secrets[f"{app}-env"] == {"FEATURE_X": VALUE}  # only PASS keys, never OTHER
    assert secrets[f"{db}-conn"] == {
        "host": f"{db}.{result.namespace}.svc",
        "port": "5432",
        "name": "ordersdb",
        "username": "ordersuser",
    }
    assert f"{db}-auth" in secrets and f"{db}-url" in secrets
    assert not any("DB_HOST" in v or "old" in str(v) for v in secrets.values())


def test_every_variable_reaches_the_app_as_a_secret_reference(tmp_path):
    store, ref, plan = world(tmp_path)
    result = prepare(FakeRunner(), store, ref, plan)
    app = next(r for r in result.request["resources"] if r["kind"] == "APPLICATION")
    db = result.releases["orders-db"]
    bindings = app["env_bindings"]
    ref_of = {k: v["secret_ref"] for k, v in bindings.items() if "secret_ref" in v}
    assert ref_of["DB_HOST"] == {"name": f"{db}-conn", "key": "host"}
    # ENV-04 E4: password/URL keys use the DB output binding, never the `-url` Secret directly
    assert bindings["DB_PASSWORD"] == {"resource_id": "orders-db", "output": "password_secret_ref"}
    assert bindings["DATABASE_URL"] == {
        "resource_id": "orders-db",
        "output": "connection_url_secret_ref",
    }
    assert ref_of["FEATURE_X"] == {
        "name": f"{result.releases['orders-api']}-env",
        "key": "FEATURE_X",
    }
    assert "AWS_REGION" not in ref_of


def test_no_value_reaches_a_command_the_plan_or_its_dump(tmp_path):
    store, ref, plan = world(tmp_path)
    runner = FakeRunner()
    result = prepare(runner, store, ref, plan)
    flat = " ".join(" ".join(a) for a, _ in runner.calls) + result.model_dump_json()
    assert VALUE not in flat


def test_the_proxy_variables_join_the_app_secret_when_the_network_is_on(tmp_path):
    store, ref, plan = world(tmp_path)
    runner = FakeRunner()
    result = prepare(runner, store, ref, plan, network=True)
    env = created(runner)[f"{result.releases['orders-api']}-env"]
    assert env["FEATURE_X"] == VALUE and "HTTP_PROXY" in env


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        (
            EnvPlanEntry(key="DB_DSN", disposition="UNREPRESENTABLE", reason="DSN_FORMAT"),
            "ENV_UNREPRESENTABLE",
        ),
        (
            EnvPlanEntry(key="NEVER_SET", disposition="MISSING", reason="NOT_DECLARED"),
            "ENV_MISSING",
        ),
    ],
)
def test_a_key_that_needs_a_decision_stops_the_sandbox_with_its_code(tmp_path, entry, code):
    store, ref, _ = world(tmp_path)
    plan = EnvPlan(env_file_ref=ref, entries=[*entries(), entry])
    runner = FakeRunner()
    with pytest.raises(SandboxError, match=code):
        prepare(runner, store, ref, plan)
    assert runner.commands("create") == []  # nothing was made


def test_an_expired_env_file_stops_the_sandbox_with_a_code_and_no_value(tmp_path):
    store, ref, plan = world(tmp_path)
    store.expire(ref)
    with pytest.raises(SandboxError, match="ENV_FILE_EXPIRED") as exc:
        prepare(FakeRunner(), store, ref, plan)
    assert VALUE not in str(exc.value)


def test_a_plan_for_another_env_file_is_refused(tmp_path):
    store, _, plan = world(tmp_path)
    other = store.put(b"A=1\n")
    with pytest.raises(SandboxError, match="ENV_PLAN_REF_MISMATCH"):
        prepare(FakeRunner(), store, other, plan)


def test_without_a_plan_the_original_file_path_still_works(tmp_path):
    from .test_deploy import prepare as legacy

    runner = FakeRunner()
    plan = legacy(make_service(runner), tmp_path, url=True)
    assert created(runner)[f"{plan.releases['orders-api']}-env"] == {"FEATURE_X": ENV_SECRET_VALUE}


# ------------------------------------------------------------------ the Activity


@pytest.mark.asyncio
async def test_the_prepare_activity_renders_from_the_plan_the_analysis_stored(tmp_path):
    from cmp_contracts.verification import canonical_requirements
    from cmp_worker.activities.sandbox_loop import SandboxInput, SandboxLoopActivities
    from cmp_worker.artifact_store import LocalArtifactStore

    store, ref, plan = world(tmp_path)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    req_ref = artifacts.put(
        canonical_requirements(requirements(True))[0], ref_id="requirements", kind="requirements"
    )
    plan_ref = artifacts.put_json(plan, ref_id="env-plan", kind="env-plan")
    suite_ref = artifacts.put(b"{}", ref_id="suite", kind="test-suite")
    data = SandboxInput(
        migration_id="mig-1", run_id="run1", requirements_ref=req_ref, suite_ref=suite_ref,
        source={"workspace": str(tmp_path)}, database_image=PG,
        env_file_ref=ref, env_plan_ref=plan_ref,
    )  # fmt: skip
    runner = FakeRunner()
    activities = SandboxLoopActivities(
        service=make_service(runner), store=artifacts, workspace_root=tmp_path / "w",
        build=lambda *_: None, suite_runner=lambda *_: None, env_store=store,
    )  # fmt: skip
    payload = {
        "input": data.model_dump(mode="json"), "requirements_ref": req_ref.model_dump(),
        "images": {"orders-api": APP.model_dump(mode="json")},
    }  # fmt: skip
    out = await activities.sbx_prepare(payload)
    assert out["sandbox_error"] is None and VALUE not in json.dumps(out)
    assert any("-conn" in name for name in created(runner))
    # without the store the Sandbox stops with a code instead of reading a local file
    none = SandboxLoopActivities(
        service=make_service(FakeRunner()), store=artifacts, workspace_root=tmp_path / "w",
        build=lambda *_: None, suite_runner=lambda *_: None,
    )  # fmt: skip
    assert (await none.sbx_prepare(payload))["sandbox_error"] == "ENV_FILE_STORE_UNCONFIGURED"


def test_the_sandbox_and_production_render_the_same_except_for_the_database_references(tmp_path):
    from cmp_contracts.shared_deployment_v2 import SecretRefV2
    from cmp_providers.env_file import DbOutputRefs, database_outputs, db_output_refs, render_env

    store, ref, plan = world(tmp_path)
    sandbox = render_env(store, plan, ref, db_output_refs("db-sbx"), app_release="app-sbx")
    outputs = database_outputs(
        release="db-prod", namespace="prod", name="ordersdb", username="ordersuser",
        password_secret_ref=SecretRefV2(name="db-prod-pw", key="pw"),
    )  # fmt: skip
    production = render_env(
        store, plan, ref, DbOutputRefs.from_outputs(outputs, database_release="db-prod"),
        app_release="app-sbx",
    )  # fmt: skip
    passed = lambda r: [e for e in r.env if e.name == "FEATURE_X"]
    assert passed(sandbox) == passed(production) and sandbox.secrets == production.secrets
    assert {e.name for e in sandbox.env} == {e.name for e in production.env}
    assert [e.secret_ref for e in sandbox.env if e.name == "DB_HOST"] != [
        e.secret_ref for e in production.env if e.name == "DB_HOST"
    ]


# ------------------------------------------------------------------ shared renderer (E4)


def _secret_refs(result) -> dict[str, tuple[str, str]]:
    """The app's final `secretKeyRef`s (name, key) from the shared v2 renderer's Helm Values."""
    app = next(r for r in result.resources if r.kind == "APPLICATION")
    return {
        e.name: (e.source.name, e.source.key)
        for e in app.helm_values.env
        if e.source.kind == "SECRET"
    }


def _render(request):
    from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2

    return render_shared_deployment_v2(
        request,
        database_chart_dir=ROOT / "infra/helm/postgres",
        application_chart_dir=ROOT / "infra/helm/application",
    )


def test_an_env_file_sandbox_passes_the_shared_renderer_with_e4_references(tmp_path, caplog):
    """Demo regression: `.env` with DATABASE_URL used to fail with SECRET_NAME_CONFLICT."""
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    store, ref, plan = world(tmp_path)
    result = prepare(FakeRunner(), store, ref, plan)
    rendered = _render(result.request)
    db, app = result.releases["orders-db"], result.releases["orders-api"]
    assert _secret_refs(rendered) == {
        "DATABASE_URL": (f"{db}-url", "url"),
        "DB_PASSWORD": (f"{db}-auth", "password"),
        "DB_HOST": (f"{db}-conn", "host"),
        "FEATURE_X": (f"{app}-env", "FEATURE_X"),
    }
    flat = rendered.model_dump_json() + "".join(r.manifest_yaml for r in rendered.resources)
    assert VALUE not in flat and VALUE not in caplog.text


def test_the_sandbox_and_production_end_at_the_same_secret_references(tmp_path):
    """Same EnvPlan → same final `secretKeyRef` for the password/URL keys in both paths."""
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    from cmp_contracts.shared_deployment_v2 import SecretRefV2
    from cmp_providers.env_file import DbOutputRefs, database_outputs, preview_env
    from cmp_worker.plan_publish import _env_binding

    store, ref, plan = world(tmp_path)
    sandbox = prepare(FakeRunner(), store, ref, plan)
    db, app = sandbox.releases["orders-db"], sandbox.releases["orders-api"]
    outputs = database_outputs(
        release=db, namespace=sandbox.namespace, name="ordersdb", username="ordersuser",
        password_secret_ref=SecretRefV2(name=f"{db}-auth", key="password"),
    )  # fmt: skip
    previewed = preview_env(
        store, plan, ref, DbOutputRefs.from_outputs(outputs, database_release=db), app_release=app
    )
    entries_by_key = {e.key: e for e in plan.entries}
    production = json.loads(json.dumps(sandbox.request))
    production["environment"] = "PRODUCTION"
    target = next(r for r in production["resources"] if r["kind"] == "APPLICATION")
    target["env_bindings"] = {
        item.name: _env_binding(entries_by_key[item.name], item, "orders-db")
        for item in previewed.env
    }
    sandbox_refs, production_refs = (
        _secret_refs(_render(sandbox.request)),
        _secret_refs(_render(production)),
    )
    assert sandbox_refs == production_refs
    assert sandbox_refs["DATABASE_URL"] == (f"{db}-url", "url")
    assert sandbox_refs["DB_PASSWORD"] == (f"{db}-auth", "password")
