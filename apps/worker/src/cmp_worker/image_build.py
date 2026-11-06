"""Common S3 archive → ECR image builder used before Sandbox execution.

AWS permissions come from the build worker's IAM identity. No credential or
Docker build argument is accepted by this API or written to Temporal history.
"""

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from urllib.parse import urlsplit

from cmp_contracts.provisioning_v2 import S3ArchiveRef
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ECR = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com"
    r"(?P<china>\.cn)?/(?P<repo>[a-z0-9]+(?:[._/-][a-z0-9]+)*)$"
)
_ARCHIVE = re.compile(r"^(?P<sha>[a-f0-9]{64})\.tar\.gz$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_BLOCKED_PARTS = {".env", ".git", ".aws", ".kube", ".docker", ".ssh"}
_BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_MAX_FILES = 20000
# Ticket 54: RUN steps use the daemon's default bridge (`docker0`); the demo host drops traffic from
# there to the instance metadata service (infra/demo/build-imds-guard.sh). Never `host`, and no
# `--allow network.host`, so a Dockerfile `RUN --network=host` is refused by BuildKit.
BUILD_NETWORK = "default"
# Selecting another builder (e.g. a docker-container or remote BuildKit) would leave `docker0`.
_BUILDER_SELECTORS = ("BUILDX_BUILDER",)
# Ticket 58: the platforms an image may be built for. One build runs per platform on the daemon's
# own builder (QEMU/binfmt for the non-native one); several are joined into one manifest list.
PLATFORMS = ("linux/amd64", "linux/arm64")


def parse_platforms(value: str | list[str] | tuple[str, ...]) -> list[str]:
    """`BUILD_PLATFORM`: one platform or a comma list, in order (the first is the Sandbox's).

    Unknown, repeated or empty entries: `BUILD_PLATFORM_INVALID` (a settings error)."""
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value) if isinstance(value, (list, tuple)) else [value]
    items = [part.strip() if isinstance(part, str) else part for part in parts]
    if not items or any(item not in PLATFORMS for item in items) or len(set(items)) != len(items):
        raise ValueError("BUILD_PLATFORM_INVALID")
    return items


def single_platform_input(data: Any) -> Any:
    """Reads the pre-58 single `platform` input as `platforms` (old callers keep working)."""
    if isinstance(data, dict) and "platform" in data and "platforms" not in data:
        data = dict(data)
        data["platforms"] = parse_platforms(data.pop("platform"))
    return data


class _Platforms(BaseModel):
    """`platforms` (ticket 58): the image's platforms, in order. `platform` is the first one."""

    platforms: list[str]

    @model_validator(mode="before")
    @classmethod
    def _single_platform(cls, data: Any) -> Any:
        return single_platform_input(data)

    @field_validator("platforms", mode="before")
    @classmethod
    def _valid_platforms(cls, value: Any) -> list[str]:
        return parse_platforms(value)

    @property
    def platform(self) -> str:
        return self.platforms[0]


class S3Ref(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    uri: str
    version_id: Annotated[str, Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def valid_uri(self) -> "S3Ref":
        split = urlsplit(self.uri)
        if split.scheme != "s3" or not split.netloc or not split.path.strip("/"):
            raise ValueError("invalid S3 URI")
        if split.query or split.fragment or ".." in PurePosixPath(split.path).parts:
            raise ValueError("invalid S3 URI")
        return self

    @property
    def bucket(self) -> str:
        return urlsplit(self.uri).netloc

    @property
    def key(self) -> str:
        return urlsplit(self.uri).path.lstrip("/")


class BuildContext(_Platforms):
    """Execution metadata supplied by orchestration and worker configuration."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    migration_id: str
    resource_id: str
    attempt_id: str
    ecr_repository: str
    log_bucket: str

    @model_validator(mode="after")
    def valid_context(self) -> "BuildContext":
        if any(
            not _NAME.fullmatch(value)
            for value in (self.migration_id, self.resource_id, self.attempt_id)
        ):
            raise ValueError("invalid migration, resource or attempt ID")
        if not _ECR.fullmatch(self.ecr_repository):
            raise ValueError("invalid ECR repository URI")
        if not _BUCKET.fullmatch(self.log_bucket):
            raise ValueError("invalid log bucket")
        return self


class _BuildRequest(_Platforms):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    migration_id: str = Field(min_length=1)
    resource_id: str
    attempt_id: str
    source: S3Ref
    ecr_repository: str
    log_destination: S3Ref
    # Ticket 47 (B-20): named AWS profile for the `aws` calls (the worker has no global one).
    aws_profile: str | None = None

    @model_validator(mode="after")
    def valid_request(self) -> "_BuildRequest":
        if any(
            not _NAME.fullmatch(value)
            for value in (self.migration_id, self.resource_id, self.attempt_id)
        ):
            raise ValueError("invalid migration, resource or attempt ID")
        if not _ECR.fullmatch(self.ecr_repository):
            raise ValueError("invalid ECR repository URI")
        if not _ARCHIVE.fullmatch(PurePosixPath(self.source.key).name):
            raise ValueError("source archive name must be its SHA-256")
        if self.log_destination.version_id is not None:
            raise ValueError("log destination cannot specify a version")
        if self.log_destination.uri == self.source.uri:
            raise ValueError("log destination cannot overwrite source")
        return self


class BuildOutcome(_Platforms):
    """`image_digest` is the manifest list's when there are several platforms (ticket 58)."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    migration_id: str
    resource_id: str
    attempt_id: str
    status: str
    source_ref: S3ArchiveRef
    image_digest: str | None = None
    image_uri: str | None = None
    log: S3Ref | None = None
    error_code: str | None = None

    @property
    def source(self) -> S3Ref:
        """Compatibility view for callers of the initial common builder."""
        return S3Ref(uri=self.source_ref.uri, version_id=self.source_ref.version_id)

    @property
    def source_sha256(self) -> str:
        return self.source_ref.sha256

    @property
    def digest(self) -> str | None:
        return self.image_digest


class BuildFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # CLI output can contain source content or credentials; never forward it.
        raise BuildFailure("EXTERNAL_COMMAND_FAILED") from None


def _aws_env(profile: str | None) -> dict[str, str] | None:
    """Environment for `aws` calls: the process one plus the Build profile when configured."""
    return dict(os.environ, AWS_PROFILE=profile) if profile else None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(source: S3Ref, destination: Path, env: dict[str, str] | None = None) -> None:
    command = ["aws", "s3api", "head-object", "--bucket", source.bucket, "--key", source.key]
    if source.version_id:
        command += ["--version-id", source.version_id]
    try:
        size = json.loads(_run(command + ["--output", "json"], env=env).stdout)["ContentLength"]
    except (ValueError, KeyError, TypeError):
        raise BuildFailure("SOURCE_METADATA_INVALID") from None
    if not isinstance(size, int) or size < 0:
        raise BuildFailure("SOURCE_METADATA_INVALID")
    if size > _MAX_ARCHIVE_BYTES:
        raise BuildFailure("ARCHIVE_TOO_LARGE")
    command[2] = "get-object"
    _run(command + [str(destination)], env=env)
    if destination.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise BuildFailure("ARCHIVE_TOO_LARGE")
    expected = _ARCHIVE.fullmatch(PurePosixPath(source.key).name).group("sha")
    if _hash_file(destination) != expected:
        raise BuildFailure("SOURCE_DIGEST_MISMATCH")


def _extract(archive: Path, context: Path) -> None:
    count = 0
    expanded = 0
    seen = set()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or not path.parts
                    or ".." in path.parts
                    or str(path) in seen
                    or any(
                        part in _BLOCKED_PARTS or part.startswith(".env.") for part in path.parts
                    )
                    or path.suffix in _BLOCKED_SUFFIXES
                ):
                    raise BuildFailure("UNSAFE_ARCHIVE")
                seen.add(str(path))
                if member.isdir():
                    (context / member.name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise BuildFailure("UNSAFE_ARCHIVE")
                if member.size < 0:
                    raise BuildFailure("UNSAFE_ARCHIVE")
                count += 1
                expanded += member.size
                if count > _MAX_FILES or expanded > _MAX_EXPANDED_BYTES:
                    raise BuildFailure("ARCHIVE_TOO_LARGE")
                output = context / member.name
                output.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, output.open("wb") as target:
                    while block := source.read(1024 * 1024):
                        target.write(block)
    except (OSError, tarfile.TarError):
        raise BuildFailure("INVALID_ARCHIVE") from None
    if not (context / "Dockerfile").is_file():
        raise BuildFailure("DOCKERFILE_MISSING")


def _ecr_digest(request: _BuildRequest, tag: str) -> str | None:
    match = _ECR.fullmatch(request.ecr_repository)
    command = [
        "aws",
        "ecr",
        "describe-images",
        "--region",
        match.group("region"),
        "--repository-name",
        match.group("repo"),
        "--image-ids",
        f"imageTag={tag}",
        "--output",
        "json",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=_aws_env(request.aws_profile),
        )
    except subprocess.CalledProcessError as error:
        if "ImageNotFoundException" in (error.stderr or ""):
            return None
        raise BuildFailure("ECR_LOOKUP_FAILED") from None
    except (OSError, subprocess.TimeoutExpired):
        raise BuildFailure("ECR_LOOKUP_FAILED") from None
    try:
        digest = json.loads(result.stdout)["imageDetails"][0]["imageDigest"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise BuildFailure("ECR_LOOKUP_INVALID") from None
    if not _DIGEST.fullmatch(digest):
        raise BuildFailure("ECR_LOOKUP_INVALID")
    return digest


def _require_immutable_ecr(request: _BuildRequest) -> None:
    match = _ECR.fullmatch(request.ecr_repository)
    result = _run(
        [
            "aws",
            "ecr",
            "describe-repositories",
            "--region",
            match.group("region"),
            "--repository-names",
            match.group("repo"),
            "--output",
            "json",
        ],
        env=_aws_env(request.aws_profile),
    )
    try:
        repository = json.loads(result.stdout)["repositories"][0]
        immutable = repository["imageTagMutability"] == "IMMUTABLE"
        account = repository["registryId"] == match.group("account")
    except (ValueError, KeyError, IndexError, TypeError):
        raise BuildFailure("ECR_REPOSITORY_INVALID") from None
    if not immutable or not account:
        raise BuildFailure("ECR_REPOSITORY_NOT_IMMUTABLE")


def _docker_env(docker_config: Path) -> dict[str, str]:
    """Process environment for Docker CLI calls: a fresh per-build config, the daemon's builder."""
    env = {key: value for key, value in os.environ.items() if key not in _BUILDER_SELECTORS}
    env["DOCKER_CONFIG"] = str(docker_config)
    return env


def _buildx_command(*, platform: str, context: Path, image: str, metadata: Path) -> list[str]:
    return [
        "docker",
        "buildx",
        "build",
        "--push",
        "--progress",
        "quiet",
        "--network",
        BUILD_NETWORK,
        "--platform",
        platform,
        "--file",
        str(context / "Dockerfile"),
        "--tag",
        image,
        "--metadata-file",
        str(metadata),
        str(context),
    ]


def _login(request: _BuildRequest, docker_config: Path) -> dict[str, str]:
    match = _ECR.fullmatch(request.ecr_repository)
    env = _docker_env(docker_config)
    password = _run(
        [
            "aws",
            "ecr",
            "get-login-password",
            "--region",
            match.group("region"),
        ],
        env=_aws_env(request.aws_profile),
    ).stdout
    _run(
        [
            "docker",
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            request.ecr_repository.split("/", 1)[0],
        ],
        env=env,
        input_text=password,
    )
    return env


def _push_platform(
    request: _BuildRequest, context: Path, env: dict[str, str], platform: str, tag: str
) -> str:
    """One platform's image, built on the daemon's own builder and pushed under `tag`."""
    metadata = context.parent / f"build-metadata-{platform.replace('/', '-')}.json"
    _run(
        _buildx_command(
            platform=platform,
            context=context,
            image=f"{request.ecr_repository}:{tag}",
            metadata=metadata,
        ),
        env=env,
    )
    try:
        digest = json.loads(metadata.read_text())["containerimage.digest"]
    except (OSError, ValueError, KeyError, TypeError):
        raise BuildFailure("BUILD_DIGEST_MISSING") from None
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise BuildFailure("BUILD_DIGEST_INVALID")
    observed = _ecr_digest(request, tag)
    if observed != digest:
        raise BuildFailure("ECR_DIGEST_MISMATCH")
    return digest


def _platform_tag(tag: str, platform: str) -> str:
    return f"{tag}-{platform.rsplit('/', 1)[1]}"


def _index_platforms(raw: str) -> set[str]:
    """`os/architecture` of a manifest list's images; attestations (`unknown`) are not images."""
    try:
        manifests = json.loads(raw)["manifests"]
        found = {f"{m['platform']['os']}/{m['platform']['architecture']}" for m in manifests}
    except (ValueError, KeyError, TypeError):
        raise BuildFailure("BUILD_INDEX_INVALID") from None
    return found - {"unknown/unknown"}


def _push_index(request: _BuildRequest, env: dict[str, str], digests: list[str], tag: str) -> str:
    """Ticket 58: join the platform images into one manifest list under `tag`; its digest."""
    repository = request.ecr_repository
    _run(
        ["docker", "buildx", "imagetools", "create", "--progress", "quiet",
         "--tag", f"{repository}:{tag}", *[f"{repository}@{digest}" for digest in digests]],
        env=env,
    )  # fmt: skip
    digest = _ecr_digest(request, tag)
    if digest is None:
        raise BuildFailure("ECR_DIGEST_MISMATCH")
    raw = _run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", f"{repository}@{digest}"], env=env
    ).stdout
    if _index_platforms(raw) != set(request.platforms):
        raise BuildFailure("BUILD_INDEX_PLATFORM_MISMATCH")
    return digest


def _publish(request: _BuildRequest, context: Path, docker_config: Path, tag: str) -> str:
    env = _login(request, docker_config)
    if len(request.platforms) == 1:  # pre-58 behavior: one image under the tag, no list
        return _push_platform(request, context, env, request.platform, tag)
    digests = []
    for platform in request.platforms:
        platform_tag = _platform_tag(tag, platform)
        # ECR tags are immutable: a platform image a failed earlier try pushed is reused.
        digests.append(
            _ecr_digest(request, platform_tag)
            or _push_platform(request, context, env, platform, platform_tag)
        )
    return _push_index(request, env, digests, tag)


def _upload_log(request: _BuildRequest, record: dict, workspace: Path) -> S3Ref:
    path = workspace / "build-log.json"
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
    result = _run(
        [
            "aws",
            "s3api",
            "put-object",
            "--bucket",
            request.log_destination.bucket,
            "--key",
            request.log_destination.key,
            "--body",
            str(path),
            "--output",
            "json",
        ],
        env=_aws_env(request.aws_profile),
    )
    try:
        version_id = json.loads(result.stdout).get("VersionId")
    except ValueError:
        raise BuildFailure("LOG_UPLOAD_INVALID") from None
    return S3Ref(uri=request.log_destination.uri, version_id=version_id)


def _build_and_push(request: _BuildRequest) -> BuildOutcome:
    """Return a safe result; no raw AWS/Docker output is exposed."""
    sha = _ARCHIVE.fullmatch(PurePosixPath(request.source.key).name).group("sha")
    identity = (
        f"{request.migration_id}\0{request.resource_id}\0"
        f"{request.attempt_id}\0{','.join(request.platforms)}\0{sha}"
    )
    tag = "cmp-" + hashlib.sha256(identity.encode()).hexdigest()
    digest = None
    error_code = None
    log = None
    with tempfile.TemporaryDirectory(prefix="cmp-build-") as temporary:
        workspace = Path(temporary)
        docker_config = workspace / "docker-config"
        docker_config.mkdir(mode=0o700)
        try:
            archive = workspace / "source.tar.gz"
            _download(request.source, archive, _aws_env(request.aws_profile))
            _require_immutable_ecr(request)
            digest = _ecr_digest(request, tag)
            if digest is None:
                context = workspace / "context"
                context.mkdir()
                _extract(archive, context)
                digest = _publish(request, context, docker_config, tag)
        except BuildFailure as failure:
            error_code = failure.code
        try:
            log = _upload_log(
                request,
                {
                    "migration_id": request.migration_id,
                    "resource_id": request.resource_id,
                    "attempt_id": request.attempt_id,
                    "status": "FAILED" if error_code else "SUCCEEDED",
                    "error_code": error_code,
                    "source_sha256": sha,
                    "platform": request.platform,
                    "platforms": request.platforms,
                    "digest": digest,
                },
                workspace,
            )
        except BuildFailure:
            error_code = error_code or "LOG_UPLOAD_FAILED"
    return BuildOutcome(
        migration_id=request.migration_id,
        resource_id=request.resource_id,
        attempt_id=request.attempt_id,
        status="FAILED" if error_code else "SUCCEEDED",
        source_ref=S3ArchiveRef(
            uri=request.source.uri,
            version_id=request.source.version_id,
            sha256=sha,
        ),
        platforms=request.platforms,
        image_digest=digest if not error_code else None,
        image_uri=f"{request.ecr_repository}@{digest}" if digest and not error_code else None,
        log=log,
        error_code=error_code,
    )


class ImageBuilder:
    """Prepared per attempt; the Sandbox loop supplies only an S3 source URI."""

    def __init__(self, context: BuildContext, *, aws_profile: str | None = None) -> None:
        self.context = context
        self._aws_profile = aws_profile

    def build(self, source_uri: str, *, source_version_id: str | None = None) -> BuildOutcome:
        request = _BuildRequest(
            migration_id=self.context.migration_id,
            resource_id=self.context.resource_id,
            attempt_id=self.context.attempt_id,
            source=S3Ref(uri=source_uri, version_id=source_version_id),
            ecr_repository=self.context.ecr_repository,
            platforms=self.context.platforms,
            log_destination=S3Ref(
                uri=(
                    f"s3://{self.context.log_bucket}/builds/"
                    f"{self.context.migration_id}/{self.context.resource_id}/"
                    f"{self.context.attempt_id}.json"
                )
            ),
            aws_profile=self._aws_profile,
        )
        return _build_and_push(request)
