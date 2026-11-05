"""Reproducible, hash-named source archives for the Build module (AV-009).

The archive is built from the original workspace (not the masked copy) and uploaded as
`<prefix>/<sha256>.tar.gz`. Secrets, VCS data, links and special files never enter it, and
the result must pass the same checks the Build module applies when it unpacks it.
"""

import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from cmp_contracts.provisioning_v2 import ResourceId, S3ArchiveRef
from pydantic import TypeAdapter, ValidationError

MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_FILES = 20000
_BLOCKED_PARTS = {".env", ".git", ".aws", ".kube", ".docker", ".ssh"}
_BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_RESOURCE_ID = TypeAdapter(ResourceId)


class SourceArchiveError(Exception):
    """`code` is a stable machine-readable reason; messages never carry file contents."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ArchiveStore(Protocol):
    def exists(self, bucket: str, key: str) -> bool: ...

    def put(self, bucket: str, key: str, data: bytes) -> str | None: ...


@dataclass(frozen=True)
class SourcePackage:
    resource_id: str
    ref: S3ArchiveRef
    uploaded: bool
    file_count: int
    size: int


# ------------------------------------------------------------------ .dockerignore


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def _ignore_rules(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = (line[1:] if negated else line).strip().lstrip("/").rstrip("/")
        if pattern:
            rules.append((negated, _glob_regex(pattern)))
    return rules


def _ignored(path: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]  # path and its parents
    ignored = False
    for negated, regex in rules:
        if any(regex.fullmatch(candidate) for candidate in candidates):
            ignored = not negated
    return ignored


# --------------------------------------------------------------------- packaging


def _blocked(path: PurePosixPath) -> bool:
    return (
        any(part in _BLOCKED_PARTS or part.startswith(".env.") for part in path.parts)
        or path.suffix in _BLOCKED_SUFFIXES
    )


def _collect(context: Path, dockerfile: Path) -> dict[str, Path]:
    dockerignore = context / ".dockerignore"
    rules = (
        _ignore_rules(dockerignore.read_text(errors="ignore"))
        if dockerignore.is_file() and not dockerignore.is_symlink()
        else []
    )
    entries: dict[str, Path] = {}
    for current, dirnames, filenames in os.walk(context, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _BLOCKED_PARTS)
        for name in sorted(filenames):
            path = Path(current) / name
            relative = PurePosixPath(path.relative_to(context).as_posix())
            if _blocked(relative) or not stat.S_ISREG(path.lstat().st_mode):
                continue
            if _ignored(str(relative), rules):
                continue
            entries[str(relative)] = path
            if len(entries) > MAX_FILES:
                raise SourceArchiveError("ARCHIVE_TOO_LARGE")
    entries["Dockerfile"] = dockerfile  # always sent, placed at the archive root
    return entries


def _build(entries: dict[str, Path]) -> bytes:
    expanded = 0
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        for name in sorted(entries):
            path = entries[name]
            size = path.stat().st_size
            expanded += size
            if expanded > MAX_EXPANDED_BYTES:
                raise SourceArchiveError("ARCHIVE_TOO_LARGE")
            info = tarfile.TarInfo(name)
            info.size = size
            info.mtime = 0
            info.mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as stream:
                tar.addfile(info, stream)
    data = buffer.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise SourceArchiveError("ARCHIVE_TOO_LARGE")
    return data


def package_source(
    workspace: Path | str,
    *,
    resource_id: str,
    store: ArchiveStore,
    bucket: str,
    context: str = ".",
    dockerfile: str = "Dockerfile",
    prefix: str = "sources",
) -> SourcePackage:
    try:
        _RESOURCE_ID.validate_python(resource_id)
    except ValidationError:
        raise SourceArchiveError("INVALID_RESOURCE_ID") from None
    if not _BUCKET.fullmatch(bucket) or not _PREFIX.fullmatch(prefix):
        raise SourceArchiveError("INVALID_DESTINATION")
    root = Path(workspace).resolve()
    context_dir = (root / context).resolve()
    if context_dir != root and root not in context_dir.parents:
        raise SourceArchiveError("CONTEXT_OUTSIDE_WORKSPACE")
    if not context_dir.is_dir():
        raise SourceArchiveError("CONTEXT_MISSING")
    dockerfile_path = context_dir / dockerfile
    resolved = dockerfile_path.resolve()
    inside = resolved == context_dir or context_dir in resolved.parents
    if not inside or dockerfile_path.is_symlink() or not dockerfile_path.is_file():
        raise SourceArchiveError("DOCKERFILE_MISSING")

    entries = _collect(context_dir, dockerfile_path)
    data = _build(entries)
    sha = hashlib.sha256(data).hexdigest()
    key = f"{prefix}/{sha}.tar.gz"
    uploaded, version_id = False, None
    if not store.exists(bucket, key):
        version_id = store.put(bucket, key, data)
        uploaded = True
    ref = S3ArchiveRef(uri=f"s3://{bucket}/{key}", version_id=version_id, sha256=sha)
    return SourcePackage(resource_id, ref, uploaded, len(entries), len(data))


def build_source_refs(packages: list[SourcePackage]) -> dict[str, S3ArchiveRef]:
    refs: dict[str, S3ArchiveRef] = {}
    for package in packages:
        if package.resource_id in refs:
            raise SourceArchiveError("DUPLICATE_RESOURCE_ID")
        refs[package.resource_id] = package.ref
    return refs


# ------------------------------------------------------------------ S3 through the AWS CLI


class AwsCliArchiveStore:
    """Uses the worker's own IAM identity through the AWS CLI, like the Build module.

    No credential is accepted or placed on a command line; errors never echo CLI output.
    """

    def __init__(self, *, aws_bin: str = "aws", profile: str | None = None, timeout: int = 300):
        self._aws = aws_bin
        self._profile = profile
        self._timeout = timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        command = [self._aws, *(["--profile", self._profile] if self._profile else []), *args]
        try:
            return subprocess.run(
                command, capture_output=True, text=True, timeout=self._timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SourceArchiveError("STORE_ERROR") from None

    def exists(self, bucket: str, key: str) -> bool:
        result = self._run(["s3api", "head-object", "--bucket", bucket, "--key", key])
        if result.returncode == 0:
            return True
        if "404" in result.stderr or "Not Found" in result.stderr:
            return False
        raise SourceArchiveError("STORE_ERROR")

    def put(self, bucket: str, key: str, data: bytes) -> str | None:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".tar.gz") as body:
            body.write(data)
            body.flush()
            result = self._run(
                [
                    "s3api", "put-object", "--bucket", bucket, "--key", key,
                    "--body", body.name, "--output", "json",
                ]
            )  # fmt: skip
        if result.returncode != 0:
            raise SourceArchiveError("STORE_ERROR")
        try:
            return json.loads(result.stdout or "{}").get("VersionId")
        except ValueError:
            raise SourceArchiveError("STORE_ERROR") from None
