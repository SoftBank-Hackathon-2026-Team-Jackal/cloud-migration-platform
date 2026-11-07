"""실패한 kubectl/psql의 stderr를 값 없이 유형 코드로 남긴다(출력 자체는 남기지 않는다)."""

from __future__ import annotations

import logging
import sys

import pytest
from cmp_worker.activities import k8s_workloads_cli as cli
from cmp_worker.activities.k8s_workloads_cli import CommandError, CommandRunner, failure_reason


@pytest.fixture(autouse=True)
def _logger_enabled(monkeypatch):
    """다른 시험이 부른 alembic `fileConfig`가 기존 로거를 끈다(disable_existing_loggers): 순서와 상관없이 본다."""
    monkeypatch.setattr(cli.log, "disabled", False)


@pytest.mark.parametrize(
    "stderr,code",
    [
        (b'ERROR:  relation "cmp_switchover_marker_id_seq" already exists\n', "OBJECT_EXISTS"),
        (b'Error from server (NotFound): pods "chat-db-0" not found\n', "NOT_FOUND"),
        (
            b"Error from server (Forbidden): pods is forbidden: User cannot list\n",
            "PERMISSION_DENIED",
        ),
        (b"error: You must be logged in to the server (Unauthorized)\n", "UNAUTHORIZED"),
        (b"dial tcp 10.0.0.1:443: i/o timeout\n", "TIMEOUT"),
        (b"psql: error: connection to server failed: Connection refused\n", "CONNECT_FAILED"),
        (b"something else entirely\n", "OTHER"),
        (b"", "OTHER"),
        (None, "OTHER"),
    ],
)
def test_failure_reason_codes(stderr, code) -> None:
    assert failure_reason(stderr) == code


def test_a_failed_command_logs_only_tool_subcommand_exit_and_reason(caplog) -> None:
    secret = "s3cr3t-value"
    script = (
        f"import sys; sys.stderr.write('ERROR:  relation x already exists {secret}'); sys.exit(3)"
    )
    with caplog.at_level(logging.WARNING), pytest.raises(CommandError) as caught:
        CommandRunner().run([sys.executable, "-c", script])
    assert caught.value.reason == "OBJECT_EXISTS"
    text = caplog.text
    assert "reason=OBJECT_EXISTS" in text and "exit=3" in text
    assert secret not in text and "relation" not in text


def test_only_known_subcommands_are_logged(caplog, tmp_path) -> None:
    fake = tmp_path / "kubectl"
    fake.write_text("#!/bin/sh\necho 'Error from server (NotFound): x' >&2\nexit 1\n")
    fake.chmod(0o755)
    with caplog.at_level(logging.WARNING), pytest.raises(CommandError):
        CommandRunner().run([str(fake), "--kubeconfig", "/secret/path/kc.yaml", "exec", "-i", "pod-1",
                             "--", "sh", "-c", "psql -c 'select secret'"])  # fmt: skip
    assert "command failed: kubectl exec exit=1 reason=NOT_FOUND" in caplog.text
    assert "/secret/path" not in caplog.text and "select" not in caplog.text
