"""Code fixes leave the platform as a patch artifact, not as a pushed branch (AV-019, 14C).

A `git`-format patch is made between the source that was analysed and the source that passed.
The platform never pushes: whoever receives the patch applies it and opens the PR. The tree hash
ties the patch to what the Sandbox verified: applying it to the base gives the passed hash.
"""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cmp_scanners.masking import mask_text

_GIT = [
    "git",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.safecrlf=false",
    "-c",
    "core.hooksPath=/dev/null",
]
_BRANCH = re.compile(r"[^a-zA-Z0-9._/-]+")


class PatchError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def tree_sha256(root: Path | str) -> str:
    """SHA-256 over sorted (relative path, file SHA-256); `.git` is not part of the source."""
    base = Path(root)
    entries = []
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        rel = path.relative_to(base).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        entries.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _git(args: list[str], cwd: Path, *, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(
        [*_GIT, *args], cwd=cwd, input=stdin, capture_output=True, check=False, timeout=120
    )
    if done.returncode != 0:
        raise PatchError("GIT_FAILED")
    return done.stdout


def _copy(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)


def make_patch(base: Path | str, result: Path | str) -> bytes:
    """`git diff --binary` between two trees; empty bytes when nothing changed."""
    with tempfile.TemporaryDirectory(prefix="cmp-patch-") as tmp:
        work = Path(tmp) / "repo"
        work.mkdir()
        _copy(Path(base), work)
        _git(["init", "-q"], work)
        _git(["add", "-A", "-f"], work)
        _git(["-c", "user.name=cmp", "-c", "user.email=cmp@localhost", "commit", "-q",
              "--allow-empty", "-m", "base"], work)  # fmt: skip
        for item in work.iterdir():
            if item.name != ".git":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        _copy(Path(result), work)
        _git(["add", "-A", "-f"], work)
        return _git(["diff", "--cached", "--binary", "--no-color", "--src-prefix=a/",
                     "--dst-prefix=b/"], work)  # fmt: skip


def apply_patch_tree_sha256(base: Path | str, patch: bytes) -> str:
    """Apply the patch to a copy of the base and return the resulting tree hash."""
    with tempfile.TemporaryDirectory(prefix="cmp-apply-") as tmp:
        work = Path(tmp) / "tree"
        work.mkdir()
        _copy(Path(base), work)
        if patch:
            _git(["init", "-q"], work)
            _git(["apply", "--whitespace=nowarn", "-"], work, stdin=patch)
            shutil.rmtree(work / ".git")
        return tree_sha256(work)


@dataclass(frozen=True)
class PatchArtifact:
    branch: str
    title: str
    body: str
    patch: bytes
    base_tree_sha256: str
    result_tree_sha256: str


def branch_name(migration_id: str) -> str:
    return "cmp/" + _BRANCH.sub("-", migration_id).strip("-./")[:60] + "-migration-fix"


def build_patch_artifact(
    base: Path | str, result: Path | str, *, migration_id: str, reasons: list[str]
) -> PatchArtifact | None:
    """None when the code did not change. Raises PatchError if the patch does not reproduce
    the passed source, so a patch that would not match what was verified is never offered."""
    patch = make_patch(base, result)
    if not patch:
        return None
    base_hash, result_hash = tree_sha256(base), tree_sha256(result)
    if apply_patch_tree_sha256(base, patch) != result_hash:
        raise PatchError("PATCH_DOES_NOT_REPRODUCE_RESULT")
    notes = "\n".join(f"- {mask_text(r)}" for r in reasons) or "- (no reason recorded)"
    return PatchArtifact(
        branch=branch_name(migration_id),
        title=f"fix: changes the Sandbox needed to run {migration_id} on the target",
        body=(
            "Changes made while verifying the migration in the Sandbox. They are not applied "
            "to any branch by the platform.\n\n" + notes + f"\n\nSource tree after the patch: "
            f"`{result_hash}`"
        ),
        patch=patch,
        base_tree_sha256=base_hash,
        result_tree_sha256=result_hash,
    )


class DraftPrPublisher(Protocol):
    def publish(self, artifact: PatchArtifact, patch_ref: dict) -> dict: ...


class PreparedOnlyPublisher:
    """Default: nothing leaves the platform. The caller records that a patch is ready."""

    def publish(self, artifact: PatchArtifact, patch_ref: dict) -> dict:
        return {"status": "PREPARED", "branch": artifact.branch}
