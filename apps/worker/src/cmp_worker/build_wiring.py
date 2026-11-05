"""The real build path for the Sandbox loop (ticket 25): source to S3, Build module, evidence.

Everything here is configuration plus glue over modules that already exist: `package_source`
(AV-009), `request_build` (AV-010) and the Build module itself (called through `builder_factory`,
so D-12 can still move it). Only names of buckets, repositories and one AWS profile are
configured. No credential is accepted, stored or passed in an argument.
"""

import gzip
import hashlib
import io
import tarfile
from collections.abc import Callable
from typing import Any

from cmp_contracts.provisioning import Image
from cmp_contracts.provisioning_v2 import S3ArchiveRef
from cmp_contracts.verification import BuildAttempt, BuildLogRef
from pydantic import BaseModel, ConfigDict

from cmp_worker.build_request import (
    BuildResult,
    BuildStore,
    BuildTarget,
    request_build,
    to_build_evidence,
)
from cmp_worker.image_build import ImageBuilder
from cmp_worker.source_archive import ArchiveStore, SourceArchiveError, package_source


class BuildFailed(Exception):
    """The build produced no image. `code` and `failure_class` are stable and safe to show."""

    def __init__(self, code: str, failure_class: str):
        super().__init__(code)
        self.code = code
        self.failure_class = failure_class


class BuildSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_bucket: str
    ecr_repository: str
    log_bucket: str
    platform: str
    aws_profile: str | None = None

    @classmethod
    def from_values(
        cls,
        source_bucket: str | None,
        ecr_repository: str | None,
        log_bucket: str | None,
        platform: str | None,
        aws_profile: str | None,
    ) -> "BuildSettings | None":
        """None when nothing is set (the loop keeps its pre-built images); error when partial."""
        required = (source_bucket, ecr_repository, log_bucket, platform)
        if not any(required):
            return None
        if not all(required):
            raise ValueError("BUILD_SETTINGS_INCOMPLETE")
        return cls(
            source_bucket=source_bucket,
            ecr_repository=ecr_repository,
            log_bucket=log_bucket,
            platform=platform,
            aws_profile=aws_profile,
        )


class ImageBuildAdapter:
    """`build_with_evidence` packages the source, builds it and returns the image with its result."""

    def __init__(
        self,
        settings: BuildSettings,
        *,
        archive_store: ArchiveStore,
        build_store: BuildStore,
        builder_factory: Callable[[Any], Any] = ImageBuilder,
    ) -> None:
        self.settings = settings
        self.archive_store = archive_store
        self.build_store = build_store
        self.builder_factory = builder_factory

    def build_with_evidence(
        self,
        resource_id: str,
        source: dict[str, Any],
        *,
        migration_id: str,
        run_id: str,
        attempt: int,
    ) -> tuple[Image, BuildResult]:
        try:
            package = package_source(
                source["workspace"],
                resource_id=resource_id,
                store=self.archive_store,
                bucket=self.settings.source_bucket,
            )
        except SourceArchiveError as exc:
            raise BuildFailed(exc.code, "SOURCE") from None
        result = request_build(
            run_id=run_id,
            attempt=attempt,
            resource_id=resource_id,
            source=package.ref,
            target=BuildTarget(
                migration_id=migration_id,
                ecr_repository=self.settings.ecr_repository,
                log_bucket=self.settings.log_bucket,
                platform=self.settings.platform,
            ),
            store=self.build_store,
            builder_factory=self.builder_factory,
        )
        if result.status != "SUCCEEDED":
            raise BuildFailed(result.error_code or "BUILD_FAILED", result.failure_class or "BUILD")
        return Image(uri=result.image_uri, platforms=[result.platform]), result


def to_build_attempt(result: BuildResult, *, migration_id: str) -> BuildAttempt:
    evidence = to_build_evidence(result)
    return BuildAttempt(
        migration_id=migration_id,
        resource_id=result.resource_id,
        attempt_id=result.attempt_id,
        result_ref=evidence.result_ref,
        status=result.status,
        source_ref=result.source_ref,
        platform=result.platform,
        image_uri=result.image_uri,
        image_digest=result.image_digest,
        log=BuildLogRef(uri=result.log) if result.log else None,
        error_code=result.error_code,
    )


def suite_archive(bundle: bytes) -> bytes:
    """The pinned suite bundle as a reproducible tar.gz: same bundle, same bytes, same hash."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("suite.json")
        info.size, info.mtime, info.mode = len(bundle), 0, 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        tar.addfile(info, io.BytesIO(bundle))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def upload_suite(
    store: ArchiveStore, bucket: str, bundle: bytes, *, prefix: str = "tests"
) -> S3ArchiveRef:
    data = suite_archive(bundle)
    sha = hashlib.sha256(data).hexdigest()
    key = f"{prefix}/{sha}.tar.gz"
    version_id = None
    if not store.exists(bucket, key):
        version_id = store.put(bucket, key, data)
    return S3ArchiveRef(uri=f"s3://{bucket}/{key}", version_id=version_id, sha256=sha)
