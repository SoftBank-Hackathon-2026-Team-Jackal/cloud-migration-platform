"""Look at and clean up Sandbox namespaces (ticket 24). Pure helpers over `SandboxService`.

Only namespaces named `cmp-sbx-*` that carry this service's label are ever listed or deleted.
Nothing here prints a secret: a namespace, a run ID and an expiry time are all it shows.
"""

from datetime import UTC, datetime
from typing import Any

from cmp_worker.sandbox_deploy import SandboxService

CONFIRM_ALL = "DELETE-ALL"


def format_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Sandbox가 없습니다."
    lines = []
    for row in rows:
        expires = row["expires_at"]
        when = datetime.fromtimestamp(expires, UTC).isoformat() if expires is not None else "없음"
        state = "만료" if row["expired"] else "유효"
        lines.append(f"{row['namespace']}  run={row['run_id'] or '-'}  만료={when}  {state}")
    return "\n".join(lines)


def cleanup_sandboxes(
    service: SandboxService,
    *,
    now: datetime | None = None,
    everything: bool = False,
    confirm: str = "",
) -> list[str]:
    """Delete expired Sandboxes; `everything` also deletes the live ones and needs `DELETE-ALL`."""
    if not everything:
        return service.cleanup_expired(now=now)
    if confirm != CONFIRM_ALL:
        raise ValueError("CONFIRMATION_REQUIRED")
    deleted = []
    for row in service.list_sandboxes(now=now):
        service.teardown_sandbox(row["namespace"])
        deleted.append(row["namespace"])
    return deleted


def run_command(
    argv: list[str], service: SandboxService, *, now: datetime | None = None
) -> tuple[int, str]:
    """`status`, `cleanup`, or `cleanup --all --confirm DELETE-ALL`. Returns (exit code, text)."""
    command = argv[0] if argv else ""
    rest = argv[1:]
    if command == "status" and not rest:
        return 0, format_status(service.list_sandboxes(now=now))
    if command == "cleanup":
        everything = "--all" in rest
        confirm = rest[rest.index("--confirm") + 1] if "--confirm" in rest[:-1] else ""
        try:
            deleted = cleanup_sandboxes(service, now=now, everything=everything, confirm=confirm)
        except ValueError:
            return 2, f"--all은 `--confirm {CONFIRM_ALL}`가 있어야 합니다."
        if not deleted:
            return 0, "지울 Sandbox가 없습니다."
        return 0, "지운 Sandbox:\n" + "\n".join(deleted)
    return 2, "사용법: status | cleanup [--all --confirm DELETE-ALL]"
