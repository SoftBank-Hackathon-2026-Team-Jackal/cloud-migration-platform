"""ENV-03 (#147): shared `.env` reference functions — EnvPlan, resolve_env_names, render_env."""

import logging
from pathlib import Path

import pytest
from cmp_contracts.env_plan import (
    EnvDecision,
    EnvPlan,
    EnvPlanEntry,
    to_env_exceptions,
)
from cmp_contracts.provisioning import ContentRef
from cmp_contracts.shared_deployment_v2 import DatabaseOutputsV2, SecretRefV2
from cmp_providers.env_file import (
    DbOutputRefs,
    EnvFileError,
    EnvFileStore,
    InMemoryEnvFileStore,
    connection_secret_data,
    connection_secret_name,
    db_output_refs,
    render_env,
    resolve_env_names,
)
from cmp_providers.env_plan_builder import build_env_plan
from cmp_scanners.envfile import parse_env_names
from pydantic import ValidationError

ENV_TEXT = """\
# comment
export APP_MODE=production
SECRET_TOKEN="tok-very-secret-1234"
DB_HOST=old-rds.abc.amazonaws.com
DB_PORT=5432
DB_NAME=shop
DB_USER=shopuser
DB_PASSWORD='pw-very-secret-5678'
DATABASE_URL=postgresql://shopuser:pw-very-secret-5678@old/shop
DB_DSN=host=old user=shopuser password=pw-very-secret-5678
AWS_ACCESS_KEY_ID=AKIAVERYSECRET000000
"""
VALUES = ["production", "tok-very-secret-1234", "pw-very-secret-5678", "AKIAVERYSECRET000000"]


@pytest.fixture
def store():
    return InMemoryEnvFileStore()


@pytest.fixture
def ref(store):
    return store.put(ENV_TEXT.encode())


def entry(key, disposition, binding=None, reason=None):
    return EnvPlanEntry(key=key, disposition=disposition, binding=binding, reason=reason)


def sandbox_outputs():
    return db_output_refs("db-shop")


def target_outputs():
    outputs = DatabaseOutputsV2(
        host="db-shop-prod.app.svc",
        port=5432,
        name="shop",
        username="shopuser",
        password_secret_ref=SecretRefV2(name="shop-db-password", key="pw"),
        connection_url_secret_ref=SecretRefV2(name="db-shop-prod-url", key="url"),
    )
    return DbOutputRefs.from_outputs(outputs, database_release="db-shop-prod")


def no_values_in(text):
    return all(v not in text for v in VALUES)


# ------------------------------------------------------------------- store + names


def test_in_memory_store_satisfies_the_protocol(store):
    assert isinstance(store, EnvFileStore)


def test_store_parses_names_with_the_analysis_rules(store, ref):
    assert store.read_names(ref) == parse_env_names(ENV_TEXT)


def test_store_values_strip_matching_quotes_and_last_duplicate_wins(store):
    ref = store.put(b"A='one'\nB=\"two\"\nC=plain\nA=three\n")
    assert store.read_values(ref, ["A", "B", "C", "Z"]) == {"A": "three", "B": "two", "C": "plain"}


def test_store_errors_are_structured(store, ref):
    with pytest.raises(EnvFileError) as missing:
        store.read_names(ContentRef(id="nope", version="1", sha256="0" * 64))
    assert missing.value.code == "ENV_FILE_NOT_FOUND"
    with pytest.raises(EnvFileError) as digest:
        store.read_names(ref.model_copy(update={"sha256": "0" * 64}))
    assert digest.value.code == "ENV_FILE_DIGEST_MISMATCH"
    store.expire(ref)
    with pytest.raises(EnvFileError) as expired:
        store.read_values(ref, ["APP_MODE"])
    assert expired.value.code == "ENV_FILE_EXPIRED"
    assert str(expired.value) == "ENV_FILE_EXPIRED"


def test_reupload_under_the_same_id_is_a_new_version(store, ref):
    second = store.put(b"ONLY=1\n", file_id=ref.id)
    assert (second.id, second.version) == (ref.id, "2")
    assert store.read_names(second) == ["ONLY"]
    assert store.read_names(ref) == parse_env_names(ENV_TEXT)


def test_resolve_env_names_never_reads_values(store, ref):
    assert resolve_env_names(store, ref) == parse_env_names(ENV_TEXT)
    assert store.read_values_calls == 0


def test_store_repr_has_no_values(store, ref):
    assert no_values_in(repr(store))


# -------------------------------------------------------------------- EnvPlan contract


def test_binding_is_required_exactly_for_replaced():
    with pytest.raises(ValidationError):
        entry("DB_HOST", "REPLACED")
    with pytest.raises(ValidationError):
        entry("APP_MODE", "PASS", binding="host")


def test_plan_keys_are_unique(ref):
    with pytest.raises(ValidationError):
        EnvPlan(env_file_ref=ref, entries=[entry("A", "PASS"), entry("A", "EXCLUDED")])


# ------------------------------------------------------------------------- builder


@pytest.mark.parametrize(
    ("key", "binding"),
    [
        ("DB_HOST", "host"),
        ("PGHOST", "host"),
        ("DB_PORT", "port"),
        ("DB_NAME", "name"),
        ("POSTGRES_DB", "name"),
        ("PGDATABASE", "name"),
        ("DB_USER", "username"),
        ("DB_USERNAME", "username"),
        ("DB_PASSWORD", "password_secret_ref"),
        ("DATABASE_URL", "connection_url_secret_ref"),
        ("DB_URI", "connection_url_secret_ref"),
    ],
)
def test_agreed_binding_table(ref, key, binding):
    plan = build_env_plan(ref, [key], [key])
    assert plan.entries == [entry(key, "REPLACED", binding, "DB_OUTPUT_BINDING")]


def test_builder_classifies_every_disposition(ref):
    plan = build_env_plan(
        ref,
        env_names=["APP_MODE", "AWS_ACCESS_KEY_ID", "DB_HOST", "DB_DSN"],
        code_env_reads=["APP_MODE", "DB_HOST", "DB_DSN", "FEATURE_FLAG"],
    )
    assert {e.key: (e.disposition, e.binding, e.reason) for e in plan.entries} == {
        "APP_MODE": ("PASS", None, None),
        "AWS_ACCESS_KEY_ID": ("EXCLUDED", None, "SOURCE_ONLY_CREDENTIAL"),
        "DB_HOST": ("REPLACED", "host", "DB_OUTPUT_BINDING"),
        "DB_DSN": ("UNREPRESENTABLE", None, "DSN_FORMAT"),
        "FEATURE_FLAG": ("MISSING", None, "NOT_DECLARED"),
    }
    assert [e.key for e in plan.entries] == sorted(e.key for e in plan.entries)


def test_without_a_database_db_names_pass_like_the_analysis(ref):
    plan = build_env_plan(ref, ["DB_HOST", "DB_DSN"], [], has_database=False)
    assert [(e.key, e.disposition) for e in plan.entries] == [
        ("DB_DSN", "PASS"),
        ("DB_HOST", "PASS"),
    ]


def test_env_exceptions_parity_with_build_requirements(tmp_path, ref):
    from cmp_scanners.assembly import build_requirements
    from cmp_scanners.code import scan_code
    from cmp_scanners.go_adapter import GoAdapter
    from cmp_scanners.manifests import scan_manifests

    # Same fixture as tests/analysis/test_requirements.py.
    manifests_dir = Path(__file__).parent / "fixtures/analysis/assembly/manifests"
    env = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "AWS_REGION", "LEGACY"]
    env += ["DB_DSN", "DATABASE_URL"]
    reads = [*env[:6], "FEATURE_FLAG", "DB_DSN", "PG_URI", "AWS_SECRET_ACCESS_KEY"]
    body = "package main\n" + "".join(f'var _ = os.Getenv("{n}")\n' for n in reads)
    (tmp_path / "main.go").write_text('import "github.com/jackc/pgx/v5"\n' + body)
    manifests = scan_manifests(manifests_dir)
    code = scan_code(tmp_path, {".env": env}, [GoAdapter()])
    expected = build_requirements(manifests, code, {".env": env}).env_exceptions
    # Declared names as build_requirements sees them: `.env` plus app manifest env names.
    databases = {p.resource for p in manifests.postgres_images}
    declared = set(env) | {
        n
        for w in manifests.workloads
        if w.kind in {"Deployment", "StatefulSet"} and f"{w.kind}/{w.name}" not in databases
        for n in w.env_names
    }
    code_reads = {s.target for s in code.call_sites if s.kind == "ENV_READ"}
    plan = build_env_plan(ref, sorted(declared), sorted(code_reads))
    assert to_env_exceptions(plan, db_resource_id="shop-db") == expected
    assert expected.replaced and expected.excluded and expected.missing


def test_env_exceptions_need_the_db_resource_id_for_replaced(ref):
    plan = EnvPlan(env_file_ref=ref, entries=[entry("DB_HOST", "REPLACED", "host")])
    with pytest.raises(ValueError, match="DB_RESOURCE_ID_REQUIRED"):
        to_env_exceptions(plan)


# ---------------------------------------------------------------------- render_env


def full_plan(ref):
    return EnvPlan(
        env_file_ref=ref,
        entries=[
            entry("APP_MODE", "PASS"),
            entry("SECRET_TOKEN", "PASS"),
            entry("AWS_ACCESS_KEY_ID", "EXCLUDED", reason="SOURCE_ONLY_CREDENTIAL"),
            entry("DB_HOST", "REPLACED", "host"),
            entry("DB_PORT", "REPLACED", "port"),
            entry("DB_NAME", "REPLACED", "name"),
            entry("DB_USER", "REPLACED", "username"),
            entry("DB_PASSWORD", "REPLACED", "password_secret_ref"),
            entry("DATABASE_URL", "REPLACED", "connection_url_secret_ref"),
        ],
    )


def env_map(rendered):
    return {e.name: (e.secret_ref.name, e.secret_ref.key) for e in rendered.env}


def test_render_pass_replaced_and_excluded(store, ref):
    rendered = render_env(store, full_plan(ref), ref, sandbox_outputs(), app_release="app-shop")
    assert env_map(rendered) == {
        "APP_MODE": ("app-shop-env", "APP_MODE"),
        "SECRET_TOKEN": ("app-shop-env", "SECRET_TOKEN"),
        "DB_HOST": ("db-shop-conn", "host"),
        "DB_PORT": ("db-shop-conn", "port"),
        "DB_NAME": ("db-shop-conn", "name"),
        "DB_USER": ("db-shop-conn", "username"),
        "DB_PASSWORD": ("db-shop-auth", "password"),
        "DATABASE_URL": ("db-shop-url", "url"),
    }
    assert [s.name for s in rendered.secrets] == ["app-shop-env"]
    assert rendered.secrets[0].reveal() == {
        "APP_MODE": "production",
        "SECRET_TOKEN": "tok-very-secret-1234",
    }
    assert rendered.decisions == []
    assert store.read_values_calls == 1


def test_render_reads_only_pass_keys(store, ref):
    seen = []
    original = store.read_values

    def spy(r, keys):
        seen.append(list(keys))
        return original(r, keys)

    store.read_values = spy
    render_env(store, full_plan(ref), ref, sandbox_outputs(), app_release="app-shop")
    assert seen == [["APP_MODE", "SECRET_TOKEN"]]


@pytest.mark.parametrize(
    ("plan_entry", "code"),
    [
        (entry("DB_DSN", "UNREPRESENTABLE", reason="DSN_FORMAT"), "ENV_UNREPRESENTABLE"),
        (entry("FEATURE_FLAG", "MISSING", reason="NOT_DECLARED"), "ENV_MISSING"),
        (entry("NOT_IN_FILE", "PASS"), "ENV_VALUE_MISSING"),
    ],
)
def test_blocking_entries_return_decisions_and_nothing_else(store, ref, plan_entry, code):
    plan = EnvPlan(env_file_ref=ref, entries=[entry("APP_MODE", "PASS"), plan_entry])
    rendered = render_env(store, plan, ref, sandbox_outputs(), app_release="app-shop")
    assert rendered.decisions == [EnvDecision(key=plan_entry.key, code=code)]
    assert (rendered.env, rendered.secrets) == ([], [])


def test_blocked_plans_do_not_read_values(store, ref):
    plan = EnvPlan(env_file_ref=ref, entries=[entry("DB_DSN", "UNREPRESENTABLE")])
    render_env(store, plan, ref, sandbox_outputs(), app_release="app-shop")
    assert store.read_values_calls == 0


def test_replaced_without_db_outputs_is_a_decision(store, ref):
    plan = EnvPlan(env_file_ref=ref, entries=[entry("DB_HOST", "REPLACED", "host")])
    rendered = render_env(store, plan, ref, None, app_release="app-shop")
    assert rendered.decisions == [EnvDecision(key="DB_HOST", code="ENV_DB_OUTPUT_MISSING")]


def test_plan_and_ref_must_agree(store, ref):
    other = store.put(b"X=1\n")
    with pytest.raises(EnvFileError) as error:
        render_env(store, full_plan(ref), other, sandbox_outputs(), app_release="app-shop")
    assert error.value.code == "ENV_PLAN_REF_MISMATCH"


def test_store_errors_propagate_from_render(store, ref):
    store.expire(ref)
    with pytest.raises(EnvFileError) as error:
        render_env(store, full_plan(ref), ref, sandbox_outputs(), app_release="app-shop")
    assert error.value.code == "ENV_FILE_EXPIRED"


def test_render_output_is_deterministic(store, ref):
    plan = full_plan(ref)
    shuffled = plan.model_copy(update={"entries": list(reversed(plan.entries))})
    first = render_env(store, plan, ref, sandbox_outputs(), app_release="app-shop")
    second = render_env(store, shuffled, ref, sandbox_outputs(), app_release="app-shop")
    assert first == second
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.model_dump_json() == second.model_dump_json()


def test_values_never_appear_in_repr_serialization_or_logs(store, ref, caplog):
    caplog.set_level(logging.DEBUG)
    plan = full_plan(ref).model_copy(
        update={"entries": [*full_plan(ref).entries, entry("NOT_IN_FILE", "PASS")]}
    )
    for p in (full_plan(ref), plan):
        rendered = render_env(store, p, ref, sandbox_outputs(), app_release="app-shop")
        logging.getLogger("test").info("rendered %r %s", rendered, rendered)
        for text in (
            repr(rendered),
            str(rendered),
            rendered.model_dump_json(),
            str(rendered.model_dump()),
            rendered.canonical_bytes().decode(),
            *(repr(s) for s in rendered.secrets),
        ):
            assert no_values_in(text)
    assert no_values_in(caplog.text)


def test_store_errors_have_no_values(store, ref):
    store.expire(ref)
    with pytest.raises(EnvFileError) as error:
        store.read_values(ref, ["SECRET_TOKEN"])
    assert no_values_in(repr(error.value)) and no_values_in(str(error.value))


def test_sandbox_and_target_differ_only_in_replaced_refs(store, ref):
    plan = full_plan(ref)
    sandbox = render_env(store, plan, ref, sandbox_outputs(), app_release="app-shop")
    target = render_env(store, plan, ref, target_outputs(), app_release="app-shop")
    replaced = {e.key for e in plan.entries if e.disposition == "REPLACED"}
    assert sandbox.secrets == target.secrets
    assert {k: v for k, v in env_map(sandbox).items() if k not in replaced} == {
        k: v for k, v in env_map(target).items() if k not in replaced
    }
    assert env_map(target)["DB_HOST"] == ("db-shop-prod-conn", "host")
    assert env_map(target)["DB_PASSWORD"] == ("shop-db-password", "pw")
    assert env_map(target)["DATABASE_URL"] == ("db-shop-prod-url", "url")
    assert all(env_map(sandbox)[k] != env_map(target)[k] for k in replaced)


# ------------------------------------------------------------- DB connection Secret


def test_connection_secret_shape():
    outputs = DatabaseOutputsV2(
        host="db-shop.ns.svc",
        port=5432,
        name="shop",
        username="shopuser",
        password_secret_ref=SecretRefV2(name="db-shop-auth", key="password"),
        connection_url_secret_ref=SecretRefV2(name="db-shop-url", key="url"),
    )
    assert connection_secret_name("db-shop") == "db-shop-conn"
    assert connection_secret_data(outputs) == {
        "host": "db-shop.ns.svc",
        "port": "5432",
        "name": "shop",
        "username": "shopuser",
    }
    refs = db_output_refs("db-shop")
    assert (refs.password_secret_ref.name, refs.password_secret_ref.key) == (
        "db-shop-auth",
        "password",
    )
    assert (refs.host.name, refs.host.key) == ("db-shop-conn", "host")
