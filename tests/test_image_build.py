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
                "attempt_id": "attempt-1",
                "ecr_repository": ECR,
                "platform": "linux/arm64",
                "log_bucket": "log-bucket",
            }
        )
    )


def source_uri(path: Path) -> str:
    return f"s3://source-bucket/sources/{path.name}"


def mock_aws_docker(monkeypatch, path: Path, *, existing=False):
    calls = []
    log_content = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["aws", "s3api", "head-object"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"ContentLength": path.stat().st_size}),
            )
        if command[:3] == ["aws", "s3api", "get-object"]:
            assert "--version-id" not in command
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
            if existing or any(item[:3] == ["docker", "buildx", "build"] for item in calls):
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
    assert result.digest == DIGEST
    assert result.image_uri == f"{ECR}@{DIGEST}"
    assert result.source.uri == source_uri(path)
    assert result.source.version_id is None
    assert result.log.uri == "s3://log-bucket/builds/migration-1/attempt-1.json"
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


def test_changed_archive_bytes_are_rejected_before_ecr_lookup(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n"})
    path.write_bytes(b"tampered")
    calls, logs = mock_aws_docker(monkeypatch, path)
    result = builder().build(source_uri(path))
    assert result.status == "FAILED"
    assert result.error_code == "SOURCE_DIGEST_MISMATCH"
    assert result.image_uri is None
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
