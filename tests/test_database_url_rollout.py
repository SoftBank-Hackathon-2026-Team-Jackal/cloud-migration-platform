"""Ticket 19 (#79): ON_PREM rollout job carries the URL Secret reference; the Agent builds it."""

import base64
from urllib.parse import unquote, urlsplit

import pytest
from cmp_api.agent_registry import RegisteredAgent
from cmp_api.db import Base
from cmp_api.onprem_rollout import LocalArtifactStore, build_rollout_job
from cmp_providers.onprem_agent import MANAGED
from onprem_rollout_support import approve, build_artifacts, claim_request, plan, register_agent
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_onprem_agent import FakeCluster, job, run

URL_SECRET = {"name": "demo-db-url", "key": "url"}
CONNECTION = {"secret": URL_SECRET, "database": "app db", "username": "appuser"}


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def url_job(**override):
    database = job().database.model_dump(mode="json") | {"connection_url": CONNECTION}
    return job(database=database, **override)


def url_secret_value(cluster):
    secret = cluster.objects[("secret", "demo-db-url")]
    assert secret["metadata"]["labels"] == {MANAGED: "true"}
    return base64.b64decode(secret["data"]["url"]).decode()


def password_of(cluster):
    data = cluster.objects[("secret", "demo-db-auth")]["data"]["password"]
    return base64.b64decode(data).decode()


# --- Agent (cluster double from ticket 12) ---------------------------------------------------


def test_fresh_rollout_creates_the_url_secret_from_the_generated_password():
    cluster = FakeCluster()
    assert run(cluster, url_job()) == ("SUCCEEDED", "READY")
    url = urlsplit(url_secret_value(cluster))
    assert (url.scheme, url.hostname, url.port) == ("postgresql", "demo-db.demo.svc", 5432)
    assert unquote(url.username) == "appuser" and unquote(url.path[1:]) == "app db"
    assert unquote(url.password) == password_of(cluster)
    # Password Secret first, then the URL Secret, then Helm.
    assert cluster.mutations[:2] == [("apply", "Secret"), ("apply", "Secret")]


def test_user_provided_password_is_used_and_a_stale_url_is_refreshed():
    cluster = FakeCluster()
    cluster.objects[("secret", "demo-db-auth")] = {"data": {"password": b64("p@ss/w:rd")}}
    cluster.objects[("secret", "demo-db-url")] = {
        "metadata": {"labels": {MANAGED: "true"}},
        "data": {"url": b64("postgresql://old")},
    }
    assert run(cluster, url_job()) == ("SUCCEEDED", "READY")
    assert unquote(urlsplit(url_secret_value(cluster)).password) == "p@ss/w:rd"


def test_unmanaged_secret_with_the_url_name_is_never_overwritten():
    cluster = FakeCluster()
    cluster.objects[("secret", "demo-db-url")] = {"data": {"url": b64("user-owned")}}
    assert run(cluster, url_job()) == ("FAILED", "SECRET_NAME_CONFLICT")
    assert cluster.mutations == []


def test_job_without_the_binding_creates_no_url_secret_and_keeps_its_bytes():
    cluster = FakeCluster()
    assert run(cluster) == ("SUCCEEDED", "READY")
    assert ("secret", "demo-db-url") not in cluster.objects
    assert "connection_url" not in job().model_dump(mode="json")["database"]


def test_password_secret_missing_its_key_fails_before_mutation():
    cluster = FakeCluster()
    cluster.objects[("secret", "demo-db-auth")] = {"data": {}}
    assert run(cluster, url_job()) == ("FAILED", "SECRET_REFERENCE_MISSING")
    assert cluster.mutations == []


# --- platform job builder --------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_job_carries_the_url_reference_and_does_not_treat_it_as_a_user_secret(db, tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    artifacts = build_artifacts(store, tmp_path, url_binding=True)
    with db() as session:
        register_agent(session)
        manifest = plan(artifacts)
        request = claim_request(approve(session, manifest), manifest)
        built, _ = build_rollout_job(session, request, store)
        assert session.get(RegisteredAgent, "agent-a") is not None
    assert built.database.connection_url.model_dump(mode="json") == {
        "secret": URL_SECRET,
        "database": "appdb",
        "username": "appuser",
    }
    assert URL_SECRET not in [ref.model_dump() for ref in built.application.secret_refs]
    assert "postgresql://" not in built.model_dump_json()


def test_job_without_the_binding_has_no_connection_url(db, tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    artifacts = build_artifacts(store, tmp_path)
    with db() as session:
        register_agent(session)
        manifest = plan(artifacts)
        built, _ = build_rollout_job(
            session, claim_request(approve(session, manifest), manifest), store
        )
    assert built.database.connection_url is None


def test_preview_lists_the_url_secret_as_platform_created_not_user_required(tmp_path):
    from cmp_providers.onprem_preview import onprem_preview

    store = LocalArtifactStore(tmp_path / "store")
    artifacts = build_artifacts(store, tmp_path, url_binding=True)
    manifest = plan(artifacts)
    contents = {role: store.read(manifest.artifacts[role]) for role in manifest.artifacts}
    preview = onprem_preview(manifest, contents)
    required = [w.message for w in preview.warnings if w.code == "USER_SECRET_REQUIRED"]
    assert all("demo-db-url" not in message for message in required)
    assert any("demo-db-url/url" in change.name for change in preview.applied_defaults)
    assert "postgresql://" not in preview.model_dump_json()
