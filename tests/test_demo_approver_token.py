"""infra/demo/approver-token.py: credential goes to the web env file once, not to stdout."""

import importlib.util
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "infra/demo/approver-token.py"
spec = importlib.util.spec_from_file_location("approver_token", SCRIPT)
approver_token = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approver_token)


def test_env_file_keeps_other_lines_and_replaces_token(tmp_path):
    env = tmp_path / "web.env"
    env.write_text("AUTH_GITHUB_ID=x\nPLATFORM_APPROVER_TOKEN=old\n")
    approver_token.write_env(env, "new")
    assert env.read_text() == "AUTH_GITHUB_ID=x\nPLATFORM_APPROVER_TOKEN=new\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_create_writes_token_to_env_file_only(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_call(api, method, path, key, payload=None):
        calls.append((method, path, key, payload))
        return {"approver_id": "demo-approver", "credential": "cmp_approver_secret"}

    monkeypatch.setattr(approver_token, "call", fake_call)
    monkeypatch.setenv("APPROVAL_ADMIN_KEY", "admin")
    env = tmp_path / "web.env"
    approver_token.main(
        ["create", "demo-approver", "--migration", "migration-1", "--env-file", str(env)]
    )
    assert calls == [
        (
            "POST",
            "/provisioning/approvers",
            "admin",
            {"approver_id": "demo-approver", "migration_ids": ["migration-1"]},
        )
    ]
    out = capsys.readouterr()
    assert "cmp_approver_secret" not in out.out + out.err
    assert "PLATFORM_APPROVER_TOKEN=cmp_approver_secret" in env.read_text()

    approver_token.main(["rotate", "demo-approver"])
    assert calls[-1][:2] == ("POST", "/provisioning/approvers/demo-approver/credential")
    assert capsys.readouterr().out == "cmp_approver_secret\n"


def test_create_and_grant_need_a_migration(monkeypatch):
    monkeypatch.setenv("APPROVAL_ADMIN_KEY", "admin")
    for action in ("create", "grant"):
        with pytest.raises(SystemExit):
            approver_token.main([action, "demo-approver"])
