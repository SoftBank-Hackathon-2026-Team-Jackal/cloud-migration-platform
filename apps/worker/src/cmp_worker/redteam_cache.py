"""An optional S3-backed cache of what a red-team attack already found (ticket 39 draft).

The point: a user who redeploys the *same source* repeatedly (iterating, or a rehearsal) should
not pay for and wait on a fresh model attack every time. The key is `source_sha256` -- the same
hash the build step already computes -- never the built image's own digest. Two builds of
byte-identical source do not land on the same image digest (the Go toolchain embeds a build ID
and timestamps even with `-trimpath`), so digest-keyed caching never hits on a genuine repeat; the
source hash does, because it is computed from the source bytes directly. The cache never stands
in for Finding/revalidation data (ticket 34/36 still records every Finding against its own real
image digest) -- it only short-circuits the *attack itself* before one is dispatched.

Nothing here is wired to anything yet: `SandboxSettings.redteam_cache_enabled` (default True) and
`RedTeamKit.cache` both have to be set for a run to actually use it, and a `None` cache (the
default -- no bucket configured) makes every lookup a clean, silent miss.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class RedTeamCacheError(Exception):
    """`code` is stable and safe to log; never the cached payload itself."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RedTeamCacheStore(Protocol):
    """Only get/put: a cache is never authoritative, so there is no delete or list here. A
    store that cannot be reached raises `RedTeamCacheError`; the caller treats that as a miss
    (see `cached_attack` in `redteam_stage.py`), never as a reason to fail the run."""

    def get(self, key: str) -> dict | None: ...

    def put(self, key: str, value: dict) -> None: ...


def cache_key(source_sha256: str) -> str:
    """One key per exact source, nothing else. The Findings an earlier run happened to carry are
    deliberately not part of it: the model finds different things on each run, so keying on them
    made identical source miss. The object under this key is the latest result for that source."""
    return f"redteam/attack/{source_sha256}.json"


class S3RedTeamCache:
    """Uses the worker's own IAM identity through the AWS CLI, like the Build module's
    `AwsCliArchiveStore` (`source_archive.py`) -- same reasoning: no credential ever touches a
    command line or an error message, and `aws` is what this platform already shells out to.
    """

    def __init__(
        self, *, bucket: str, prefix: str = "", aws_bin: str = "aws",
        profile: str | None = None, timeout: int = 60,
    ) -> None:  # fmt: skip
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._aws = aws_bin
        self._profile = profile
        self._timeout = timeout

    def _key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        command = [self._aws, *(["--profile", self._profile] if self._profile else []), *args]
        try:
            return subprocess.run(
                command, capture_output=True, timeout=self._timeout, check=False
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            raise RedTeamCacheError("CACHE_STORE_ERROR") from None

    def get(self, key: str) -> dict | None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "body.json"
            result = self._run(
                ["s3api", "get-object", "--bucket", self._bucket, "--key", self._key(key), str(dest)]
            )  # fmt: skip
            if result.returncode != 0:
                stderr = result.stderr.decode(errors="ignore")
                if "NoSuchKey" in stderr or "404" in stderr or "Not Found" in stderr:
                    return None
                raise RedTeamCacheError("CACHE_STORE_ERROR")
            try:
                return json.loads(dest.read_bytes())
            except ValueError:
                return None  # a corrupt or non-JSON object is a miss, not a crash

    def put(self, key: str, value: dict) -> None:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".json") as body:
            body.write(json.dumps(value).encode())
            body.flush()
            result = self._run(
                ["s3api", "put-object", "--bucket", self._bucket, "--key", self._key(key),
                 "--body", body.name, "--output", "json"],
            )  # fmt: skip
        if result.returncode != 0:
            raise RedTeamCacheError("CACHE_STORE_ERROR")


def optional_s3_cache(
    bucket: str | None, *, prefix: str = "", profile: str | None = None
) -> S3RedTeamCache | None:
    """None when no bucket is configured -- the same "off unless set" shape every other optional
    store in this codebase uses (e.g. `optional_env_file_store`)."""
    return S3RedTeamCache(bucket=bucket, prefix=prefix, profile=profile) if bucket else None
