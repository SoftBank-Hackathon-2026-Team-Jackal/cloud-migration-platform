"""Ticket 60 A12: with `DEMO_APPROVER_ID` set, `POST /migrations` grants the new Migration to that
approver (the operator grant rule of ticket 07, reused). Unset: the start is unchanged."""

from pathlib import Path

import test_web_start_request as start
from cmp_api.approvals import Approver
from cmp_api.config import settings
from cmp_api.routes import migrations as migration_routes

api, db, env_store, catalog = start.api, start.db, start.env_store, start.catalog
env_ref, agent = start.env_ref, start.agent


def add_approver(db, approver_id="demo-approver", migrations=("demo-placeholder",)):
    with db() as session:
        session.add(
            Approver(id=approver_id, credential_digest="0" * 64, migration_ids=list(migrations))
        )
        session.commit()


def grants(db, approver_id="demo-approver"):
    with db() as session:
        return session.get(Approver, approver_id).migration_ids


def test_configured_demo_approver_gets_each_new_migration(api, db, env_ref, agent, monkeypatch):
    client, fake = api
    monkeypatch.setattr(settings, "demo_approver_id", "demo-approver")
    add_approver(db)
    first = client.post("/migrations", json={"project_id": "p"})
    real = client.post("/migrations", json=start.web_request(start.ONPREM, env_ref.id))
    assert (first.status_code, real.status_code) == (202, 202)
    ids = [started["id"] for started in fake.started]
    assert grants(db) == sorted(["demo-placeholder", *ids])


def test_unset_demo_approver_changes_nothing(api, db, monkeypatch):
    client, fake = api
    monkeypatch.setattr(settings, "demo_approver_id", None)
    add_approver(db)
    assert client.post("/migrations", json={"project_id": "p"}).status_code == 202
    assert grants(db) == ["demo-placeholder"] and len(fake.started) == 1


def test_missing_demo_approver_never_blocks_the_start(api, db, monkeypatch):
    client, fake = api
    monkeypatch.setattr(settings, "demo_approver_id", "nobody")
    warnings = []
    monkeypatch.setattr(
        migration_routes.logger, "warning", lambda message, *args: warnings.append(message % args)
    )
    assert client.post("/migrations", json={"project_id": "p"}).status_code == 202
    assert len(fake.started) == 1
    assert warnings == [f"DEMO_APPROVER_GRANT_FAILED APPROVER_NOT_FOUND {fake.started[0]['id']}"]


def test_refused_start_grants_nothing(api, db, monkeypatch):
    client, fake = api
    monkeypatch.setattr(settings, "demo_approver_id", "demo-approver")
    add_approver(db)
    bad = client.post("/migrations", json=start.web_request(start.ONPREM, "envf-missing"))
    assert bad.status_code == 422 and fake.started == []
    assert grants(db) == ["demo-placeholder"]


def test_demo_compose_passes_the_setting_empty_by_default():
    import yaml

    compose_file = Path(__file__).resolve().parents[1] / "infra/demo/compose.yaml"
    compose = yaml.safe_load(compose_file.read_text())
    assert compose["services"]["api"]["environment"]["DEMO_APPROVER_ID"] == "${DEMO_APPROVER_ID:-}"
