"""Fetch source into an isolated workspace pinned to one commit (AV-002)."""

import base64
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from cmp_scanners.envfile import is_env_file, parse_env_names

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SKIP_DIRS = {".git", "node_modules", ".venv"}
_GIT_TIMEOUT = 300


class SourceFetchError(Exception):
    """Raised with a message that never contains credentials."""


class SourceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: str
    commit: str
    commit_verified: bool
    reused: bool
    env_names: dict[str, list[str]]


def default_workspace_root(configured: str | None = None) -> Path:
    return (
        Path(configured) if configured else Path(tempfile.gettempdir()) / "cmp-analysis-workspaces"
    )


def _check_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    cwd = Path.cwd().resolve()
    if resolved == cwd or cwd in resolved.parents or resolved in cwd.parents:
        raise SourceFetchError("workspace root must be outside the service process directory")
    return resolved


def _run_git(args: list[str], *, token: str | None, cwd: Path | None = None) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraheader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
        )
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            check=False,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceFetchError("git command timed out") from exc
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()[-1:] or ["git command failed"]
        text = message[0]
        if token:
            text = text.replace(token, "***")
            text = text.replace(
                base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***"
            )
        raise SourceFetchError(f"git failed: {text[:300]}")
    return result.stdout.strip()


def _env_names(workspace: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if _SKIP_DIRS.intersection(relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if is_env_file(path.name):
            found[relative.as_posix()] = parse_env_names(path.read_text(errors="ignore"))
    return found


def fetch_source(
    repository: str,
    *,
    requested_commit: str,
    migration_id: str,
    workspace_root: Path | str,
    token: str | None = None,
    resolve_production_commit: Callable[[], str | None] = lambda: None,
    allow_local: bool = False,
) -> SourceSnapshot:
    if not _SAFE_ID.fullmatch(migration_id):
        raise SourceFetchError("invalid migration id")
    if not allow_local and not repository.startswith("https://"):
        raise SourceFetchError("only https repositories are allowed")
    root = _check_root(Path(workspace_root))
    migration_dir = root / migration_id
    migration_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    migration_dir.chmod(0o700)

    production = resolve_production_commit()
    candidate = production or requested_commit
    existing = migration_dir / candidate
    if _FULL_SHA.fullmatch(candidate) and (existing / ".git").is_dir():
        return _snapshot(existing, candidate, verified=production == candidate, reused=True)

    temp = Path(tempfile.mkdtemp(prefix=".clone-", dir=migration_dir))
    try:
        _run_git(["clone", "--quiet", "--no-checkout", repository, str(temp / "repo")], token=token)
        repo = temp / "repo"
        resolved = _resolve(repo, production)
        verified = resolved is not None
        if resolved is None:
            resolved = _resolve(repo, requested_commit)
            if resolved is None:
                raise SourceFetchError("requested commit was not found")
        target = migration_dir / resolved
        if (target / ".git").is_dir():
            return _snapshot(target, resolved, verified=verified, reused=True)
        _run_git(["checkout", "--quiet", "--detach", resolved], token=None, cwd=repo)
        repo.chmod(0o700)
        repo.rename(target)
        return _snapshot(target, resolved, verified=verified, reused=False)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _resolve(repo: Path, rev: str | None) -> str | None:
    if not rev:
        return None
    try:
        return _run_git(["rev-parse", "--verify", f"{rev}^{{commit}}"], token=None, cwd=repo)
    except SourceFetchError:
        return None


def _snapshot(path: Path, commit: str, *, verified: bool, reused: bool) -> SourceSnapshot:
    return SourceSnapshot(
        workspace=str(path),
        commit=commit,
        commit_verified=verified,
        reused=reused,
        env_names=_env_names(path),
    )
