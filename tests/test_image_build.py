import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from cmp_worker.image_build import BuildContext, ImageBuilder, _extract
from pydantic import ValidationError

ECR = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/demo"
DIGEST = "sha256:" + "a" * 64


def archive(tmp_path: Path, files: dict[str, bytes]) -> Path:
    path = tmp_path / "archive.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    target = tmp_path / f"{hashlib.sha256(path.read_bytes()).hexdigest()}.tar.gz"
    path.rename(target)
    return target


def builder() -> ImageBuilder:
    return ImageBuilder(
        BuildContext.model_validate(
            {
                "migration_id": "migration-1",
                "resource_id": "api-main",
                "attempt_id": "attempt-1",
                "ecr_repository": ECR,
                "platform": "linux/arm64",
                "log_bucket": "log-bucket",
            }
        )
    )


def source_uri(path: Path) -> str:
    return f"s3://source-bucket/sources/{path.name}"


def mock_aws_docker(monkeypatch, path: Path, *, existing=False, version_id=None):
    calls = []
    log_content = []
    published_tags = set()

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["aws", "s3api", "head-object"]:
            assert (
                command[command.index("--version-id") + 1] if "--version-id" in command else None
            ) == version_id
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"ContentLength": path.stat().st_size}),
            )
        if command[:3] == ["aws", "s3api", "get-object"]:
            assert (
                command[command.index("--version-id") + 1] if "--version-id" in command else None
            ) == version_id
            shutil.copyfile(path, command[-1])
            return subprocess.CompletedProcess(command, 0, stdout="{}")
        if command[:3] == ["aws", "ecr", "describe-repositories"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "repositories": [
                            {"registryId": "123456789012", "imageTagMutability": "IMMUTABLE"}
                        ]
                    }
                ),
            )
        if command[:3] == ["aws", "ecr", "describe-images"]:
            tag = command[command.index("--image-ids") + 1]
            if existing or tag in published_tags:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"imageDetails": [{"imageDigest": DIGEST}]}),
                )
            raise subprocess.CalledProcessError(
                254,
                command,
                stderr="ImageNotFoundException: image missing",
            )
        if command[:3] == ["aws", "ecr", "get-login-password"]:
            return subprocess.CompletedProcess(command, 0, stdout="temporary-password")
        if command[:2] == ["docker", "login"]:
            assert kwargs["input"] == "temporary-password"
            assert kwargs["env"]["DOCKER_CONFIG"]
            return subprocess.CompletedProcess(command, 0, stdout="Login Succeeded")
        if command[:3] == ["docker", "buildx", "build"]:
            context = Path(command[-1])
            assert (context / "Dockerfile").exists()
            assert (context / "app.py").read_text() == "print('ok')"
            assert not (context / ".env").exists()
            Path(command[command.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.digest": DIGEST})
            )
            published_tags.add("imageTag=" + command[command.index("--tag") + 1].rsplit(":", 1)[1])
            return subprocess.CompletedProcess(command, 0, stdout=DIGEST)
        if command[:3] == ["aws", "s3api", "put-object"]:
            log_content.append(json.loads(Path(command[command.index("--body") + 1]).read_text()))
            return subprocess.CompletedProcess(command, 0, stdout='{"VersionId":"log-v1"}')
        raise AssertionError(command)

    monkeypatch.setattr("cmp_worker.image_build.subprocess.run", run)
    return calls, log_content


def test_s3_to_ecr_returns_digest_and_versioned_log(tmp_path, monkeypatch):
    path = archive(
        tmp_path,
        {
            "Dockerfile": b"FROM scratch\nCOPY app.py /app.py\n",
            "app.py": b"print('ok')",
        },
    )
    calls, logs = mock_aws_docker(monkeypatch, path)
    result = builder().build(source_uri(path))
    assert result.status == "SUCCEEDED"
    assert result.resource_id == "api-main"
    assert result.digest == DIGEST
    assert result.image_uri == f"{ECR}@{DIGEST}"
    assert result.source.uri == source_uri(path)
    assert result.source.version_id is None
    wire = result.model_dump()
    assert wire["source_ref"] == {
        "uri": source_uri(path),
        "version_id": None,
        "sha256": path.name.removesuffix(".tar.gz"),
    }
    assert wire["image_digest"] == DIGEST
    assert "source" not in wire and "digest" not in wire
    assert result.log.uri == "s3://log-bucket/builds/migration-1/api-main/attempt-1.json"
    assert result.log.version_id == "log-v1"
    assert logs[0]["source_sha256"] == path.name.removesuffix(".tar.gz")
    assert [c[:3] for c in calls].count(["docker", "buildx", "build"]) == 1
    assert not any("temporary-password" in " ".join(c) for c in calls)
    assert not any("--version-id" in c for c in calls)


def test_retry_reuses_existing_digest_without_rebuild(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n"})
    calls, _ = mock_aws_docker(monkeypatch, path, existing=True)
    result = builder().build(source_uri(path))
    assert result.status == "SUCCEEDED"
    assert result.image_uri == f"{ECR}@{DIGEST}"
    assert not any(c[0] == "docker" for c in calls)


def test_new_attempt_uses_a_new_image_tag(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    calls, _ = mock_aws_docker(monkeypatch, path)
    first = builder()
    second = ImageBuilder(first.context.model_copy(update={"attempt_id": "attempt-2"}))
    first.build(source_uri(path))
    second.build(source_uri(path))
    build_commands = [c for c in calls if c[:3] == ["docker", "buildx", "build"]]
    assert len(build_commands) == 2
    assert (
        build_commands[0][build_commands[0].index("--tag") + 1]
        != (build_commands[1][build_commands[1].index("--tag") + 1])
    )


def test_two_resources_with_same_attempt_have_distinct_tags_and_logs(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    calls, _ = mock_aws_docker(monkeypatch, path)
    first = builder()
    second = ImageBuilder(first.context.model_copy(update={"resource_id": "worker-main"}))
    first_result = first.build(source_uri(path))
    second_result = second.build(source_uri(path))
    assert first_result.log.uri != second_result.log.uri
    assert second_result.resource_id == "worker-main"
    build_commands = [c for c in calls if c[:3] == ["docker", "buildx", "build"]]
    assert len(build_commands) == 2
    assert (
        build_commands[0][build_commands[0].index("--tag") + 1]
        != build_commands[1][build_commands[1].index("--tag") + 1]
    )


def test_source_version_is_pinned_for_head_and_get(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n"})
    calls, _ = mock_aws_docker(monkeypatch, path, existing=True, version_id="source-v1")
    result = builder().build(source_uri(path), source_version_id="source-v1")
    assert result.source.version_id == "source-v1"
    assert [c for c in calls if c[:3] == ["aws", "s3api", "head-object"]]
    assert [c for c in calls if c[:3] == ["aws", "s3api", "get-object"]]


def test_changed_archive_bytes_are_rejected_before_ecr_lookup(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n"})
    path.write_bytes(b"tampered")
    calls, logs = mock_aws_docker(monkeypatch, path)
    result = builder().build(source_uri(path))
    assert result.status == "FAILED"
    assert result.error_code == "SOURCE_DIGEST_MISMATCH"
    assert result.image_uri is None
    assert result.model_dump()["image_digest"] is None
    assert not any(c[:3] == ["aws", "ecr", "describe-images"] for c in calls)
    assert logs[0]["status"] == "FAILED"


@pytest.mark.parametrize("member", ["../outside", ".env", ".ssh/id_rsa", "key.pem"])
def test_archive_rejects_unsafe_paths(tmp_path, member):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", member: b"secret"})
    context = tmp_path / "context"
    context.mkdir()
    from cmp_worker.image_build import BuildFailure

    with pytest.raises(BuildFailure, match="UNSAFE_ARCHIVE"):
        _extract(path, context)
    assert not (tmp_path / "outside").exists()


def test_archive_rejects_symlink(tmp_path):
    path = tmp_path / "archive.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("Dockerfile")
        info.size = 12
        tar.addfile(info, io.BytesIO(b"FROM scratch"))
        link = tarfile.TarInfo("app")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        tar.addfile(link)
    from cmp_worker.image_build import BuildFailure

    with pytest.raises(BuildFailure, match="UNSAFE_ARCHIVE"):
        _extract(path, tmp_path / "context")


def test_input_requires_ecr_and_hash_named_archive():
    bad = builder().context.model_dump()
    bad["ecr_repository"] = "localhost:5000/demo"
    with pytest.raises(ValidationError):
        BuildContext.model_validate(bad)
    with pytest.raises(ValidationError):
        builder().build("s3://bucket/latest.tar.gz")


def test_mutable_ecr_repository_is_rejected(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n"})
    calls, logs = mock_aws_docker(monkeypatch, path)
    original = subprocess.run

    def mutable(command, **kwargs):
        if command[:3] == ["aws", "ecr", "describe-repositories"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "repositories": [
                            {"registryId": "123456789012", "imageTagMutability": "MUTABLE"}
                        ]
                    }
                ),
            )
        return original(command, **kwargs)

    monkeypatch.setattr("cmp_worker.image_build.subprocess.run", mutable)
    result = builder().build(source_uri(path))
    assert result.status == "FAILED"
    assert result.error_code == "ECR_REPOSITORY_NOT_IMMUTABLE"
    assert result.log.version_id == "log-v1"
    assert not any(c[0] == "docker" for c in calls)
    assert logs[0]["error_code"] == result.error_code


def test_build_runs_on_daemon_default_bridge_without_network_entitlements(tmp_path, monkeypatch):
    """Ticket 54: RUN steps stay on `docker0`, where the host IMDS guard applies."""
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    monkeypatch.setenv("BUILDX_BUILDER", "unguarded-remote-builder")
    calls, _ = mock_aws_docker(monkeypatch, path)
    mocked = subprocess.run
    envs = {}

    def run(command, **kwargs):
        if command[:3] == ["docker", "buildx", "build"]:
            envs["build"] = kwargs["env"]
        return mocked(command, **kwargs)

    monkeypatch.setattr("cmp_worker.image_build.subprocess.run", run)
    assert builder().build(source_uri(path)).status == "SUCCEEDED"
    (build,) = [c for c in calls if c[:3] == ["docker", "buildx", "build"]]
    assert build[build.index("--network") + 1] == "default"
    assert not any(arg.startswith("--allow") or "network.host" in arg for arg in build)
    assert "--builder" not in build
    # A fresh DOCKER_CONFIG and no BUILDX_BUILDER: always the daemon's own `docker` driver.
    assert "BUILDX_BUILDER" not in envs["build"]
    assert Path(envs["build"]["DOCKER_CONFIG"]).name == "docker-config"


def test_buildx_command_is_shared_with_the_imds_smoke(tmp_path):
    from cmp_worker.image_build import _buildx_command

    command = _buildx_command(
        platform="linux/amd64",
        context=tmp_path,
        image=f"{ECR}:cmp-tag",
        metadata=tmp_path / "metadata.json",
    )
    assert command[:3] == ["docker", "buildx", "build"]
    assert command[command.index("--network") + 1] == "default"
    assert command[-1] == str(tmp_path)
