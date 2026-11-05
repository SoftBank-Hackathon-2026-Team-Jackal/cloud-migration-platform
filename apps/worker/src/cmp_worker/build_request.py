"""Calls the common Build module and turns its outcome into reusable, classified results (AV-010).

The Build module (B-01) is only called through `builder_factory`, so where it runs and with
which identity (D-12) can change without touching this code. Nothing here accepts or stores a
credential; results hold references and image identifiers only.
"""

import concurrent.futures
import hashlib
import re
import threading
from collections.abc import Callable
from typing import Literal, Protocol

from cmp_contracts.provisioning import Platform, VersionRef
from cmp_contracts.provisioning_v2 import BuildEvidenceV2, S3ArchiveRef
from pydantic import BaseModel, ConfigDict

from cmp_worker.image_build import BuildContext, BuildOutcome, ImageBuilder

_ATTEMPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")

_REPACKAGE = {
    "UNSAFE_ARCHIVE", "INVALID_ARCHIVE", "ARCHIVE_TOO_LARGE",
    "SOURCE_DIGEST_MISMATCH", "SOURCE_METADATA_INVALID",
}  # fmt: skip
_INFRA = {"LOG_UPLOAD_INVALID", "LOG_UPLOAD_FAILED"}
_BUILD = {"EXTERNAL_COMMAND_FAILED", "BUILD_DIGEST_MISSING", "BUILD_DIGEST_INVALID"}


def classify_failure(code: str) -> tuple[str, str]:
    """(failure class, next action) for a Build module error code."""
    if code == "DOCKERFILE_MISSING":
        return "CODE", "FIX_CODE"  # the source must be fixed
    if code in _REPACKAGE:
        return "REPACKAGE", "REPACKAGE"
    if code.startswith("ECR_") or code in _INFRA:
        return "INFRA", "RETRY_THEN_REVIEW"
    if code in _BUILD:
        return "BUILD", "BUILD_FAILED"
    return "UNKNOWN", "REVIEW"


def make_attempt_id(run_id: str, number: int) -> str:
    attempt = f"{run_id}-b{number}"
    if number < 1 or not _ATTEMPT.fullmatch(attempt):
        raise ValueError("invalid run ID or attempt number")
    return attempt


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class BuildTarget(_Strict):
    """Where to build: worker configuration plus the platform from target capabilities."""

    migration_id: str
    ecr_repository: str
    log_bucket: str
    platform: Platform


class BuildResult(_Strict):
    status: Literal["SUCCEEDED", "FAILED"]
    resource_id: str
    attempt_id: str
    attempts: int
    reused: bool = False
    platform: Platform
    source_ref: S3ArchiveRef
    image_uri: str | None = None
    image_digest: str | None = None
    error_code: str | None = None
    failure_class: str | None = None
    next_action: str | None = None
    log: str | None = None


class BuildStore(Protocol):
    def find(self, resource_id: str, source_sha256: str, platform: str) -> BuildResult | None: ...

    def save(self, result: BuildResult) -> None: ...


class InMemoryBuildStore:
    """Immutable per attempt; successful results are indexed for reuse. Real storage is D-02."""

    def __init__(self) -> None:
        self._by_attempt: dict[tuple[str, str, str], BuildResult] = {}
        self._reusable: dict[tuple[str, str, str], BuildResult] = {}
        self._lock = threading.Lock()

    def find(self, resource_id: str, source_sha256: str, platform: str) -> BuildResult | None:
        return self._reusable.get((resource_id, source_sha256, platform))

    def save(self, result: BuildResult) -> None:
        with self._lock:
            # The Build module's image tag includes the platform, so one attempt may build several.
            key = (result.resource_id, result.attempt_id, result.platform)
            existing = self._by_attempt.get(key)
            if existing is not None and existing != result:
                raise ValueError("a saved build result cannot be changed")
            self._by_attempt[key] = result
            if result.status == "SUCCEEDED":
                self._reusable.setdefault(
                    (result.resource_id, result.source_ref.sha256, result.platform), result
                )


class _Builder(Protocol):
    def build(self, source_uri: str, *, source_version_id: str | None = None) -> BuildOutcome: ...


def _invoke(
    builder: _Builder,
    source: S3ArchiveRef,
    heartbeat: Callable[[], None] | None,
    interval: float,
) -> BuildOutcome:
    if heartbeat is None:
        return builder.build(source.uri, source_version_id=source.version_id)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(builder.build, source.uri, source_version_id=source.version_id)
        while True:
            try:
                return future.result(timeout=interval)
            except concurrent.futures.TimeoutError:
                heartbeat()


def _normalize(
    outcome: BuildOutcome, *, source: S3ArchiveRef, target: BuildTarget, attempts: int
) -> BuildResult:
    digest = outcome.image_digest
    consistent = (
        outcome.status == "SUCCEEDED"
        and outcome.error_code is None
        and digest is not None
        and _DIGEST.fullmatch(digest) is not None
        and outcome.image_uri == f"{target.ecr_repository}@{digest}"
        and outcome.source_ref == source
        and outcome.platform == target.platform
    )
    common = {
        "resource_id": outcome.resource_id,
        "attempt_id": outcome.attempt_id,
        "attempts": attempts,
        "platform": target.platform,
        "source_ref": source,
        "log": outcome.log.uri if outcome.log else None,
    }
    if consistent:
        return BuildResult(
            status="SUCCEEDED", image_uri=outcome.image_uri, image_digest=digest, **common
        )
    code = outcome.error_code or "BUILD_RESULT_INCONSISTENT"
    failure_class, next_action = classify_failure(code)
    return BuildResult(
        status="FAILED",
        error_code=code,
        failure_class=failure_class,
        next_action=next_action,
        **common,
    )


def request_build(
    *,
    run_id: str,
    attempt: int,
    resource_id: str,
    source: S3ArchiveRef,
    target: BuildTarget,
    store: BuildStore,
    builder_factory: Callable[[BuildContext], _Builder] = ImageBuilder,
    heartbeat: Callable[[], None] | None = None,
    heartbeat_interval: float = 10.0,
) -> BuildResult:
    attempt_id = make_attempt_id(run_id, attempt)
    hit = store.find(resource_id, source.sha256, target.platform)
    if hit is not None:
        return hit.model_copy(update={"reused": True, "attempts": 0})
    context = BuildContext(
        migration_id=target.migration_id,
        resource_id=resource_id,
        attempt_id=attempt_id,
        ecr_repository=target.ecr_repository,
        platform=target.platform,
        log_bucket=target.log_bucket,
    )
    builder = builder_factory(context)
    attempts = 0
    while True:
        attempts += 1
        outcome = _invoke(builder, source, heartbeat, heartbeat_interval)
        # A failed external command is retried once with the same attempt ID (same image tag).
        if outcome.error_code == "EXTERNAL_COMMAND_FAILED" and attempts == 1:
            continue
        break
    result = _normalize(outcome, source=source, target=target, attempts=attempts)
    store.save(result)
    return result


def to_build_evidence(result: BuildResult) -> BuildEvidenceV2:
    """Planning-facing evidence; `result_ref` is derived from the result's content."""
    content = result.model_dump_json(exclude={"reused", "attempts"})
    platform_tag = result.platform.replace("/", "-")
    ref = VersionRef(
        id=f"build-{result.resource_id}-{result.source_ref.sha256[:12]}-{platform_tag}",
        version=hashlib.sha256(content.encode()).hexdigest()[:16],
    )
    succeeded = result.status == "SUCCEEDED"
    return BuildEvidenceV2(
        resource_id=result.resource_id,
        result_ref=ref,
        status=result.status,
        source_ref=result.source_ref,
        image_uri=result.image_uri,
        image_digest=result.image_digest,
        platforms=[result.platform] if succeeded else [],
    )
