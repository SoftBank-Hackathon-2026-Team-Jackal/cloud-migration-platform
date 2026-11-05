import hashlib
import os
from pathlib import Path

import pytest
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_scanners.assembly import (
    build_requirements,
    check_dockerfile,
    precheck,
    serialize_requirements,
)
from cmp_scanners.code import scan_code
from cmp_scanners.go_adapter import GoAdapter
from cmp_scanners.manifests import scan_manifests

MANIFESTS = Path(__file__).parent.parent / "fixtures/analysis/assembly/manifests"
GO_ENV = [
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "AWS_REGION",
    "LEGACY",
]


def go_workspace(tmp_path, reads, extra=None):
    body = "package main\n" + "".join(f'var _ = os.Getenv("{n}")\n' for n in reads)
    files = {"main.go": 'import "github.com/jackc/pgx/v5"\n' + body, **(extra or {})}
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return tmp_path


@pytest.fixture
def stack(tmp_path):
    reads = [*GO_ENV[:6], "FEATURE_FLAG"]
    workspace = go_workspace(tmp_path, reads)
    manifests = scan_manifests(MANIFESTS)
    code = scan_code(workspace, {".env": GO_ENV}, [GoAdapter()])
    return build_requirements(manifests, code, {".env": GO_ENV}), manifests, code, workspace


def by_kind(requirements, kind):
    return [r for r in requirements.resources if r.kind == kind]


def test_requirements_have_application_and_postgresql_with_db_bindings(stack):
    result = stack[0]
    RequirementsV2.model_validate(result.requirements.model_dump(mode="json"))
    app, db = (
        by_kind(result.requirements, "APPLICATION")[0],
        by_kind(result.requirements, "POSTGRESQL")[0],
    )
    assert (app.id, app.replicas, app.cpu_millicores, app.memory_mib) == ("Shop-API", 2, 500, 512)
    assert (app.port, app.health_path) == (8080, "/healthz")
    assert {k: (b.resource_id, b.output) for k, b in app.env_bindings.items()} == {
        "DB_HOST": ("shop-db", "host"),
        "DB_PORT": ("shop-db", "port"),
        "DB_NAME": ("shop-db", "name"),
        "DB_USER": ("shop-db", "username"),
        "DB_PASSWORD": ("shop-db", "password_secret_ref"),
    }
    assert (db.version, db.extensions) == ("16", [])
    assert (db.database.name, db.database.username) == ("shopdb", "shopuser")
    assert (db.cpu_millicores, db.memory_mib, db.storage_gib) == (250, 512, 20)


def test_app_to_database_network_intent_is_derived_from_the_driver(stack):
    intents = [n.model_dump() for n in stack[0].requirements.network_intent]
    assert intents == [
        {
            "source_resource_id": "Shop-API",
            "destination_resource_id": "shop-db",
            "external_host": None,
            "port": 5432,
            "protocol": "TCP",
        }
    ]


def test_env_exceptions_split_excluded_replaced_and_missing(stack):
    exceptions = stack[0].env_exceptions
    assert exceptions.excluded == ["AWS_REGION"]
    assert exceptions.replaced["DB_HOST"] == "shop-db.host"
    assert exceptions.replaced["DB_PASSWORD"] == "shop-db.password_secret_ref"
    assert exceptions.missing == ["FEATURE_FLAG"]


def test_extension_requirement_empties_requirements_and_reports_a_block(tmp_path):
    workspace = go_workspace(tmp_path, ["DB_HOST"], {"db/001.sql": "CREATE EXTENSION pgcrypto;\n"})
    code = scan_code(workspace, {}, [GoAdapter()])
    result = build_requirements(scan_manifests(MANIFESTS), code, {})
    assert result.requirements is None
    assert [(b.feature, b.evidence) for b in result.blocks] == [
        ("PostgreSQL 확장 pgcrypto", "db/001.sql:1")
    ]


def test_single_url_connection_variable_is_bound_to_the_connection_url_secret(tmp_path):
    workspace = go_workspace(tmp_path, ["DATABASE_URL", "DB_HOST"])
    names = {".env": ["DATABASE_URL"]}
    code = scan_code(workspace, names, [GoAdapter()])
    result = build_requirements(scan_manifests(MANIFESTS), code, names)
    RequirementsV2.model_validate(result.requirements.model_dump(mode="json"))
    app = by_kind(result.requirements, "APPLICATION")[0]
    binding = app.env_bindings["DATABASE_URL"]
    assert (binding.resource_id, binding.output) == ("shop-db", "connection_url_secret_ref")
    assert result.env_exceptions.replaced["DATABASE_URL"] == "shop-db.connection_url_secret_ref"
    assert not any("DATABASE_URL" in item for item in result.unmapped_items)
    assert "DATABASE_URL" not in result.env_exceptions.missing


@pytest.mark.parametrize("name", ["DB_URL", "POSTGRES_URL", "POSTGRESQL_URI", "PG_URL"])
def test_other_url_style_names_are_bound_the_same_way(tmp_path, name):
    code = scan_code(go_workspace(tmp_path, [name]), {}, [GoAdapter()])
    result = build_requirements(scan_manifests(MANIFESTS), code, {})
    app = by_kind(result.requirements, "APPLICATION")[0]
    assert app.env_bindings[name].output == "connection_url_secret_ref"


def test_key_value_dsn_variables_are_still_reported_not_bound(tmp_path):
    code = scan_code(go_workspace(tmp_path, ["DATABASE_DSN"]), {}, [GoAdapter()])
    result = build_requirements(scan_manifests(MANIFESTS), code, {})
    assert by_kind(result.requirements, "APPLICATION")[0].env_bindings == {}
    assert any("DATABASE_DSN" in item for item in result.unmapped_items)
    assert "DATABASE_DSN" not in result.env_exceptions.replaced


def test_unsupported_postgres_version_is_blocked(tmp_path):
    (tmp_path / "db.yaml").write_text(
        "apiVersion: apps/v1\nkind: StatefulSet\nmetadata: {name: old-db}\nspec:\n"
        "  template:\n    spec:\n      containers:\n        - {name: p, image: 'postgres:15.4'}\n"
    )
    result = build_requirements(
        scan_manifests(tmp_path), scan_code(go_workspace(tmp_path / "w", []), {}, []), {}
    )
    assert result.requirements is None
    assert [b.feature for b in result.blocks] == ["PostgreSQL 버전 15"]


def test_missing_values_use_named_defaults_and_are_recorded_as_assumptions(tmp_path):
    (tmp_path / "app.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: bare}\nspec:\n"
        "  template:\n    spec:\n      containers:\n        - {name: a, image: 'x:1'}\n"
    )
    result = build_requirements(
        scan_manifests(tmp_path), scan_code(go_workspace(tmp_path / "w", []), {}, []), {}
    )
    app = by_kind(result.requirements, "APPLICATION")[0]
    assert (app.replicas, app.port, app.health_path) == (1, 8080, "/")
    joined = " ".join(result.assumptions)
    for code in ("REPLICAS_NOT_DECLARED", "PORT_NOT_DECLARED", "HEALTH_PATH_NOT_DECLARED"):
        assert code in joined
    assert "CPU_NOT_DECLARED" in joined and "MEMORY_NOT_DECLARED" in joined


def test_no_application_workload_yields_no_requirements(tmp_path):
    (tmp_path / "cm.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n")
    result = build_requirements(
        scan_manifests(tmp_path), scan_code(go_workspace(tmp_path / "w", []), {}, []), {}
    )
    assert result.requirements is None
    assert any("NO_APPLICATION_WORKLOAD" in item for item in result.unmapped_items)


def test_requirements_serialization_is_stable_and_hashed(stack):
    data, ref = serialize_requirements(stack[0].requirements)
    again, again_ref = serialize_requirements(RequirementsV2.model_validate_json(data))
    assert (data, ref) == (again, again_ref)
    assert ref == hashlib.sha256(data).hexdigest()
    assert RequirementsV2.model_validate_json(data) == stack[0].requirements


def test_dockerfile_presence_location_and_missing_copy_paths(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "main.go").write_text("package main\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM golang AS build\nCOPY go.mod go.sum ./\nCOPY *.go ./\nCOPY static ./static\n"
        "COPY --from=build /out/app /app\nADD https://example.com/x.tgz /x\nCOPY $SRC ./\n"
        'COPY ["db", "./db"]\n'
    )
    check = check_dockerfile(tmp_path)
    assert check.primary == "Dockerfile" and check.dockerfiles == ["Dockerfile"]
    assert [(m.source, m.line) for m in check.missing_copy_paths] == [
        ("go.sum", 2),
        ("static", 4),
        ("db", 8),
    ]


def test_no_dockerfile_is_reported_explicitly(tmp_path):
    check = check_dockerfile(tmp_path)
    assert check.primary is None and check.dockerfiles == []


def test_nested_dockerfile_uses_its_own_directory_as_context(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc/app.py").write_text("")
    (tmp_path / "svc/Dockerfile").write_text("FROM python\nCOPY app.py .\n")
    check = check_dockerfile(tmp_path)
    assert check.primary == "svc/Dockerfile" and check.missing_copy_paths == []


def test_precheck_reports_extension_health_cloud_address_and_sessions(tmp_path):
    workspace = go_workspace(
        tmp_path,
        ["DB_HOST"],
        {
            "db/001.sql": "-- init\nCREATE EXTENSION IF NOT EXISTS citext;\n",
            "config/app.yaml": "db: shop.cabc123.ap-northeast-2.rds.amazonaws.com\n",
            "server.go": 'import "github.com/gorilla/sessions"\n',
        },
    )
    code = scan_code(workspace, {}, [GoAdapter()])
    bare = tmp_path / "m"
    bare.mkdir()
    (bare / "a.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: noprobe}\nspec:\n"
        "  template:\n    spec:\n      containers:\n        - {name: a, image: 'x:1'}\n"
    )
    findings = precheck(workspace, scan_manifests(bare), code).findings
    assert "DB_EXTENSION: citext (db/001.sql:2)" in findings
    assert "HEALTH_PATH_NOT_FOUND: Deployment/noprobe" in findings
    assert (
        "CLOUD_ADDRESS: shop.cabc123.ap-northeast-2.rds.amazonaws.com (config/app.yaml:1)"
        in findings
    )
    assert "SESSION_STATE: server.go" in findings


def test_precheck_is_empty_for_a_clean_app(stack):
    _, manifests, code, workspace = stack
    assert precheck(workspace, manifests, code).findings == []


def test_database_driver_without_a_postgres_pod_is_reported_as_outside_scope(tmp_path):
    (tmp_path / "app.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api}\nspec:\n"
        "  template:\n    spec:\n      containers:\n        - {name: a, image: 'x:1'}\n"
    )
    workspace = go_workspace(tmp_path / "w", ["DB_HOST"])
    code = scan_code(workspace, {}, [GoAdapter()])
    result = build_requirements(scan_manifests(tmp_path), code, {})
    assert any("DB_NOT_IN_CLUSTER" in item for item in result.unmapped_items)
    assert [r.kind for r in result.requirements.resources] == ["APPLICATION"]


def test_network_policy_gives_the_app_to_database_intent_without_any_code_scan(tmp_path):
    manifests = scan_manifests(Path(__file__).parent.parent / "fixtures/analysis/source-like")
    result = build_requirements(manifests, scan_code(go_workspace(tmp_path, []), {}, []), {})
    assert [
        (n.source_resource_id, n.destination_resource_id, n.port)
        for n in result.requirements.network_intent
    ] == [("orders-api", "orders-db", 5432)]


def test_driver_and_policy_intents_are_not_duplicated(tmp_path):
    manifests = scan_manifests(Path(__file__).parent.parent / "fixtures/analysis/source-like")
    code = scan_code(go_workspace(tmp_path, ["DB_HOST"]), {}, [GoAdapter()])
    result = build_requirements(manifests, code, {})
    assert len(result.requirements.network_intent) == 1


def test_names_declared_in_configmaps_and_secrets_count_as_declared(tmp_path):
    manifests = scan_manifests(Path(__file__).parent.parent / "fixtures/analysis/source-like")
    code = scan_code(
        go_workspace(tmp_path, ["DB_HOST", "APP_PORT", "WRITE_TOKEN", "NOT_DECLARED"]),
        {},
        [GoAdapter()],
    )
    result = build_requirements(manifests, code, {})
    assert result.env_exceptions.missing == ["NOT_DECLARED"]
    assert set(result.env_exceptions.replaced) == {
        "DB_HOST",
        "DB_NAME",
        "DB_PASSWORD",
        "DB_PORT",
        "DB_USER",
    }


@pytest.mark.skipif(not os.environ.get("CMP_REAL_SOURCE_YAML"), reason="local real-manifest check")
def test_real_source_manifest_builds_valid_requirements(tmp_path):
    manifests = scan_manifests(os.environ["CMP_REAL_SOURCE_YAML"])
    result = build_requirements(manifests, scan_code(go_workspace(tmp_path, []), {}, []), {})
    assert result.requirements is not None
    assert {r.kind for r in result.requirements.resources} == {"APPLICATION", "POSTGRESQL"}
    assert result.requirements.network_intent
