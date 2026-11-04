"""Ticket 19 (#79): AWS/GCP production deploy (09/11 path) creates the DB connection URL Secret."""

import json
import logging
from urllib.parse import unquote, urlsplit

import pytest
from cmp_api.db import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_aws_workloads import (
    NAMESPACE,
    FakeCluster,
    FakeRuntime,
    Plan,
    activity_for,
    app_template,
    run,
)

URL_SECRET = (NAMESPACE, "cmp-demo-db-url")


class UrlCluster(FakeCluster):
    """Adds the Secret operations the URL step needs; tracks which Secrets the platform owns."""

    def __init__(self):
        super().__init__()
        self.managed: set[tuple[str, str]] = set()

    def create_secret(self, namespace, name, data):
        super().create_secret(namespace, name, data)
        self.managed.add((namespace, name))

    def secret_managed(self, namespace, name):
        if (namespace, name) not in self.secrets:
            return None
        return (namespace, name) in self.managed

    def secret_value(self, namespace, name, key):
        return self.secrets.get((namespace, name), {}).get(key)

    def apply_secret(self, namespace, name, data):
        # Not logged to trace: the real runtime sends this only via stdin.
        self.secrets[(namespace, name)] = dict(data)
        self.managed.add((namespace, name))


def url_app():
    template = app_template()
    template["required_env"].append("DATABASE_URL")
    template["env"].append(
        {
            "name": "DATABASE_URL",
            "source": {"kind": "DATABASE", "field": "connection_url_secret_ref"},
        }
    )
    return template


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'business.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def cluster():
    return UrlCluster()


@pytest.fixture
def runtime(cluster):
    return FakeRuntime(cluster)


def url_plan(tmp_path, db):
    app = url_app()
    return Plan(tmp_path / "artifacts", db, app=app, rules_from=app)


async def test_url_secret_is_created_from_the_generated_password_and_injected(
    tmp_path, db, cluster, runtime, caplog
):
    caplog.set_level(logging.DEBUG)
    plan = url_plan(tmp_path, db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("SUCCEEDED", "WORKLOADS_READY")
    password = cluster.secrets[(NAMESPACE, "cmp-demo-db-auth")]["password"]
    url = urlsplit(cluster.secrets[URL_SECRET]["url"])
    assert (url.scheme, url.hostname, url.port) == ("postgresql", "cmp-demo-db.cmp-demo.svc", 5432)
    assert (unquote(url.username), unquote(url.password), url.path) == (
        "demoapp",
        password,
        "/demoapp",
    )
    app_values = cluster.helm_calls[1][2]
    assert app_values["database"]["url_secret_name"] == "cmp-demo-db-url"
    assert result["resource_refs"]["db_url_secret_ref"] == "cmp-demo-db-url/url"
    exposed = json.dumps(result) + "\n".join(cluster.trace) + caplog.text
    assert password not in exposed and "postgresql://" not in exposed


async def test_existing_password_is_reused_and_the_url_refreshed(tmp_path, db, cluster, runtime):
    cluster.create_secret(NAMESPACE, "cmp-demo-db-auth", {"password": "p@ss/w:rd"})
    cluster.apply_secret(NAMESPACE, "cmp-demo-db-url", {"url": "postgresql://stale"})
    plan = url_plan(tmp_path, db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert result["status"] == "SUCCEEDED"
    assert unquote(urlsplit(cluster.secrets[URL_SECRET]["url"]).password) == "p@ss/w:rd"


async def test_unmanaged_secret_with_the_url_name_blocks_before_any_change(
    tmp_path, db, cluster, runtime
):
    cluster.secrets[URL_SECRET] = {"url": "user-owned"}
    plan = url_plan(tmp_path, db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert (result["status"], result["code"]) == ("FAILED", "SECRET_NAME_CONFLICT")
    assert cluster.secrets[URL_SECRET] == {"url": "user-owned"}
    assert (NAMESPACE, "cmp-demo-db-auth") not in cluster.secrets
    assert cluster.helm_calls == []


async def test_without_the_binding_no_url_secret_is_made(tmp_path, db, cluster, runtime):
    plan = Plan(tmp_path / "artifacts", db)
    result = await run(activity_for(db, tmp_path / "artifacts", runtime), plan.request())
    assert result["status"] == "SUCCEEDED"
    assert URL_SECRET not in cluster.secrets
    assert "db_url_secret_ref" not in result["resource_refs"]
