"""Ticket 25: the real build path next to the Sandbox loop (fake archive store, fake Build module)."""

import io
import tarfile
from pathlib import PurePosixPath

import pytest
from cmp_contracts.provisioning_v2 import S3ArchiveRef
from cmp_worker.build_request import InMemoryBuildStore
from cmp_worker.build_wiring import (
    BuildFailed,
    BuildSettings,
    ImageBuildAdapter,
    suite_archive,
    to_build_attempt,
    upload_suite,
)
from cmp_worker.image_build import BuildOutcome

REPO = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/shop"
DIGEST = "sha256:" + "b" * 64
FULL = {
    "source_bucket": "cmp-sources",
    "ecr_repository": REPO,
    "log_bucket": "cmp-build-logs",
    "platform": "linux/amd64",
}


class FakeArchives:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    def exists(self, bucket, key):
        return (bucket, key) in self.objects

    def put(self, bucket, key, data):
        self.puts += 1
        self.objects[(bucket, key)] = data
        return "v1"


class FakeBuilder:
    def __init__(self, context, calls, error=None):
        self.context, self.calls, self.error = context, calls, error

    def build(self, source_uri, *, source_version_id=None):
        self.calls.append(source_uri)
        name = PurePosixPath(source_uri).name.removesuffix(".tar.gz")
        ref = S3ArchiveRef(uri=source_uri, version_id=source_version_id, sha256=name)
        c = self.context
        return BuildOutcome(
            migration_id=c.migration_id, resource_id=c.resource_id, attempt_id=c.attempt_id,
            status="FAILED" if self.error else "SUCCEEDED", source_ref=ref, platform=c.platform,
            image_digest=None if self.error else DIGEST,
            image_uri=None if self.error else f"{REPO}@{DIGEST}", error_code=self.error,
        )  # fmt: skip


def workspace(tmp_path, text="print(1)\n"):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "main.py").write_text(text)
    return {"workspace": str(tmp_path)}


def adapter(archives, calls, error=None):
    return ImageBuildAdapter(
        BuildSettings(**FULL),
        archive_store=archives,
        build_store=InMemoryBuildStore(),
        builder_factory=lambda ctx: FakeBuilder(ctx, calls, error),
    )


def test_settings_are_off_when_empty_and_refused_when_only_partly_given():
    assert BuildSettings.from_values(None, None, None, None, None) is None
    with pytest.raises(ValueError, match="BUILD_SETTINGS_INCOMPLETE"):
        BuildSettings.from_values("cmp-sources", None, None, None, None)
    ok = BuildSettings.from_values("cmp-sources", REPO, "cmp-build-logs", "linux/amd64", "build")
    assert ok.aws_profile == "build"


def test_a_successful_build_gives_a_digest_pinned_image_and_a_build_attempt(tmp_path):
    archives, calls = FakeArchives(), []
    image, result = adapter(archives, calls).build_with_evidence(
        "api", workspace(tmp_path), migration_id="mig-1", run_id="run1", attempt=1
    )
    assert image.uri == f"{REPO}@{DIGEST}" and result.status == "SUCCEEDED"
    attempt = to_build_attempt(result, migration_id="mig-1")
    assert attempt.status == "SUCCEEDED" and attempt.image_digest == DIGEST
    assert attempt.source_ref.uri.startswith("s3://cmp-sources/sources/")
    assert len(archives.objects) == 1 and calls == [attempt.source_ref.uri]


def test_the_same_source_is_uploaded_once_and_built_once(tmp_path):
    archives, calls = FakeArchives(), []
    a = adapter(archives, calls)
    src = workspace(tmp_path)
    a.build_with_evidence("api", src, migration_id="mig-1", run_id="run1", attempt=1)
    _, again = a.build_with_evidence("api", src, migration_id="mig-1", run_id="run1", attempt=2)
    assert archives.puts == 1 and len(calls) == 1 and again.reused is True


def test_changed_source_builds_again_and_keeps_the_old_evidence(tmp_path):
    archives, calls = FakeArchives(), []
    a = adapter(archives, calls)
    a.build_with_evidence(
        "api", workspace(tmp_path), migration_id="mig-1", run_id="run1", attempt=1
    )
    (tmp_path / "main.py").write_text("print(2)\n")
    a.build_with_evidence(
        "api", {"workspace": str(tmp_path)}, migration_id="mig-1", run_id="run1", attempt=2
    )
    assert len(archives.objects) == 2 and len(calls) == 2


def test_a_failed_build_raises_with_its_code_and_class(tmp_path):
    with pytest.raises(BuildFailed) as exc:
        adapter(FakeArchives(), [], error="DOCKERFILE_MISSING").build_with_evidence(
            "api", workspace(tmp_path), migration_id="mig-1", run_id="run1", attempt=1
        )
    assert exc.value.code == "DOCKERFILE_MISSING" and exc.value.failure_class == "CODE"


def test_the_suite_archive_is_reproducible_and_named_by_its_hash():
    a, b = suite_archive(b'{"name":"s"}'), suite_archive(b'{"name":"s"}')
    assert a == b
    with tarfile.open(fileobj=io.BytesIO(a), mode="r:gz") as tar:
        assert [m.name for m in tar.getmembers()] == ["suite.json"]
        assert tar.extractfile("suite.json").read() == b'{"name":"s"}'
    archives = FakeArchives()
    ref = upload_suite(archives, "cmp-sources", b'{"name":"s"}')
    again = upload_suite(archives, "cmp-sources", b'{"name":"s"}')
    assert (ref.uri, ref.sha256) == (again.uri, again.sha256) and archives.puts == 1
    assert ref.uri == f"s3://cmp-sources/tests/{ref.sha256}.tar.gz"


# ------------------------------------------------------------- the build Activity


@pytest.mark.asyncio
async def test_the_build_activity_returns_images_and_stores_the_build_attempts(tmp_path):
    from cmp_contracts.verification import canonical_requirements
    from cmp_worker.activities.sandbox_loop import SandboxLoopActivities
    from cmp_worker.artifact_store import ArtifactRef, LocalArtifactStore

    from .test_deploy import FakeRunner, make_service, requirements

    store = LocalArtifactStore(tmp_path / "store")
    req_ref = store.put(
        canonical_requirements(requirements())[0], ref_id="requirements", kind="requirements"
    )
    src = tmp_path / "src"
    src.mkdir()
    calls = []
    activities = SandboxLoopActivities(
        service=make_service(FakeRunner()), store=store, workspace_root=tmp_path / "w",
        build=adapter(FakeArchives(), calls), suite_runner=lambda *_: None,
    )  # fmt: skip
    payload = {
        "requirements_ref": req_ref.model_dump(), "source": workspace(src),
        "migration_id": "mig-1", "run_id": "run1", "attempt": 1,
    }  # fmt: skip
    out = await activities.sbx_build(payload)
    assert out["images"]["orders-api"]["uri"] == f"{REPO}@{DIGEST}"
    import json

    saved = json.loads(store.get(ArtifactRef.model_validate(out["builds_ref"])))
    assert [a["resource_id"] for a in saved] == ["orders-api"] and saved[0]["status"] == "SUCCEEDED"
    # a failed build is reported as data, not as an Activity failure
    bad = SandboxLoopActivities(
        service=make_service(FakeRunner()), store=store, workspace_root=tmp_path / "w",
        build=adapter(FakeArchives(), [], error="DOCKERFILE_MISSING"), suite_runner=lambda *_: None,
    )  # fmt: skip
    failed = await bad.sbx_build(payload)
    assert failed["build_error"] == "DOCKERFILE_MISSING" and failed["images"] is None
    # the context must be there
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError) as exc:
        await activities.sbx_build({k: v for k, v in payload.items() if k != "run_id"})
    assert exc.value.type == "BUILD_CONTEXT_MISSING" and exc.value.non_retryable
