"""Ticket 60 seam: demo approver issuance (operator key) and per-Migration grant.

The credential is written only to a 0600 file; standard output never contains it. The grant
endpoint adds Migration IDs to an existing approver without issuing a new credential.
"""

import importlib.util
import stat
from pathlib import Path

import pytest
import test_approval_gate as gate

# Ticket 07 fixtures: in-memory business DB, fake delivery, API client with the operator key.
client, db, delivery = gate.client, gate.db, gate.delivery
ADMIN_KEY, approver = gate.ADMIN_KEY, gate.approver
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/demo_approver.py"


def load_script():
    spec = importlib.util.spec_from_file_location("demo_approver", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def grant(client, approver_id, migrations, key=ADMIN_KEY):
    return client.post(
        f"/provisioning/approvers/{approver_id}/migrations",
        json={"migration_ids": list(migrations)},
        headers={"X-Approval-Admin-Key": key},
    )


def test_grant_adds_migrations_without_a_new_credential(client):
    bearer = approver(client, "demo", ("migration-001",))
    response = grant(client, "demo", ["migration-002", "migration-001"])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "approver_id": "demo",
        "migration_ids": ["migration-001", "migration-002"],
    }
    assert "credential" not in response.text
    # the same credential still authenticates (results lookups require the approver)
    lookup = client.get("/provisioning/plans/plan-x/revisions/1/result", headers=bearer)
    assert lookup.status_code == 404


def test_grant_requires_the_operator_key_and_an_existing_approver(client):
    approver(client, "demo")
    assert grant(client, "demo", ["migration-002"], key="wrong").status_code == 403
    missing = grant(client, "nobody", ["migration-002"])
    assert (missing.status_code, missing.json()["detail"]) == (404, "APPROVER_NOT_FOUND")
    assert grant(client, "demo", []).status_code == 422


class Transport:
    """Routes the script's JSON POSTs to the FastAPI test client."""

    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, url: str, body: dict, admin_key: str) -> tuple[int, dict]:
        path = url.removeprefix("http://api")
        self.calls.append(path)
        response = self.client.post(path, json=body, headers={"X-Approval-Admin-Key": admin_key})
        return response.status_code, response.json()


def test_issue_writes_the_credential_to_a_0600_file_only(client, tmp_path, capsys, monkeypatch):
    script = load_script()
    monkeypatch.setenv("APPROVAL_ADMIN_KEY", ADMIN_KEY)
    out = tmp_path / "secrets" / "demo-approver.token"
    transport = Transport(client)
    code = script.main(
        ["issue", "--api", "http://api", "--migration-id", "migration-001", "--out", str(out)],
        post=transport,
    )
    assert code == 0
    credential = out.read_text().strip()
    assert credential.startswith("cmp_approver_")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    printed = capsys.readouterr()
    assert credential not in printed.out + printed.err
    assert ADMIN_KEY not in printed.out + printed.err
    assert str(out) in printed.out
    # the stored credential is a working Bearer for the granted Migration
    assert (
        client.get(
            "/provisioning/plans/plan-x/revisions/1/result",
            headers={"Authorization": f"Bearer {credential}"},
        ).status_code
        == 404
    )

    # an existing file is never overwritten (rotating means a new approver id)
    again = script.main(
        ["issue", "--api", "http://api", "--migration-id", "migration-001", "--out", str(out)],
        post=transport,
    )
    assert again != 0 and out.read_text().strip() == credential
    assert transport.calls == ["/provisioning/approvers"]  # refused before calling the API


def test_grant_command_and_missing_admin_key(client, tmp_path, capsys, monkeypatch):
    script = load_script()
    transport = Transport(client)
    monkeypatch.delenv("APPROVAL_ADMIN_KEY", raising=False)
    assert script.main(["grant", "--api", "http://api", "--migration-id", "m-2"], post=transport)
    assert transport.calls == []
    monkeypatch.setenv("APPROVAL_ADMIN_KEY", ADMIN_KEY)
    approver(client, "demo-approver")
    code = script.main(
        ["grant", "--api", "http://api", "--migration-id", "migration-777"], post=transport
    )
    assert code == 0
    assert "migration-777" in capsys.readouterr().out


@pytest.mark.parametrize("status,detail", [(409, "APPROVER_EXISTS"), (403, "denied")])
def test_api_refusal_is_reported_by_code(tmp_path, capsys, monkeypatch, status, detail):
    script = load_script()
    monkeypatch.setenv("APPROVAL_ADMIN_KEY", ADMIN_KEY)
    out = tmp_path / "t"
    code = script.main(
        ["issue", "--api", "http://api", "--migration-id", "m", "--out", str(out)],
        post=lambda url, body, key: (status, {"detail": detail}),
    )
    assert code != 0 and not out.exists()
    assert f"HTTP {status}" in capsys.readouterr().err
