import time

import pytest
from cmp_contracts.provisioning_v2 import BuildEvidenceV2, S3ArchiveRef
from cmp_worker.build_request import (
    BuildTarget,
    InMemoryBuildStore,
    classify_failure,
    make_attempt_id,
    request_build,
    to_build_evidence,
)
from cmp_worker.image_build import BuildOutcome
from pydantic import ValidationError

REPO = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/shop"
SHA = "a" * 64
DIGEST = "sha256:" + "b" * 64
SOURCE = S3ArchiveRef(uri=f"s3://cmp-sources/sources/{SHA}.tar.gz", version_id="v1", sha256=SHA)
TARGET = BuildTarget(
    migration_id="mig-1", ecr_repository=REPO, log_bucket="cmp-build-logs", platform="linux/amd64"
)


def outcome(context, *, error=None, digest=DIGEST, source=SOURCE, uri=None):
    return BuildOutcome(
        migration_id=context.migration_id,
        resource_id=context.resource_id,
        attempt_id=context.attempt_id,
        status="FAILED" if error else "SUCCEEDED",
        source_ref=source,
        platform=context.platform,
        image_digest=None if error else digest,
        image_uri=None if error else (uri or f"{REPO}@{digest}"),
        error_code=error,
    )


class FakeBuilder:
    """Replays outcomes in order and records every call."""

    def __init__(self, context, script, calls, delay=0.0):
        self.context, self.script, self.calls, self.delay = context, script, calls, delay

    def build(self, source_uri, *, source_version_id=None):
        self.calls.append((self.context, source_uri, source_version_id))
        time.sleep(self.delay)
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return step(self.context) if callable(step) else outcome(self.context, error=step)


def run(script, *, store=None, calls=None, resource_id="api", attempt=1, target=TARGET, **kw):
    calls = calls if calls is not None else []
    delay = kw.pop("delay", 0.0)
    result = request_build(
        run_id="run1",
        attempt=attempt,
        resource_id=resource_id,
        source=SOURCE,
        target=target,
        store=store if store is not None else InMemoryBuildStore(),
        builder_factory=lambda context: FakeBuilder(context, script, calls, delay),
        **kw,
    )
    return result, calls


def test_a_second_request_for_the_same_source_and_platform_reuses_the_digest():
    store = InMemoryBuildStore()
    first, calls = run([None], store=store)
    second, _ = run([None], store=store, calls=calls, attempt=2)
    assert len(calls) == 1
    assert (first.reused, second.reused) == (False, True)
    assert second.image_digest == first.image_digest == DIGEST
    assert second.image_uri == first.image_uri


def test_a_different_platform_resource_or_source_builds_again():
    store = InMemoryBuildStore()
    _, calls = run([None], store=store)
    run(
        [None],
        store=store,
        calls=calls,
        target=TARGET.model_copy(update={"platforms": ["linux/arm64"]}),
    )
    run([None], store=store, calls=calls, resource_id="worker")
    assert len(calls) == 3


def test_failed_builds_are_never_reused():
    store = InMemoryBuildStore()
    failed, calls = run(["DOCKERFILE_MISSING"], store=store)
    assert failed.status == "FAILED"
    run([None], store=store, calls=calls, attempt=2)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("code", "failure_class", "next_action"),
    [
        ("DOCKERFILE_MISSING", "CODE", "FIX_CODE"),
        ("UNSAFE_ARCHIVE", "REPACKAGE", "REPACKAGE"),
        ("INVALID_ARCHIVE", "REPACKAGE", "REPACKAGE"),
        ("ARCHIVE_TOO_LARGE", "REPACKAGE", "REPACKAGE"),
        ("SOURCE_DIGEST_MISMATCH", "REPACKAGE", "REPACKAGE"),
        ("SOURCE_METADATA_INVALID", "REPACKAGE", "REPACKAGE"),
        ("ECR_LOOKUP_FAILED", "INFRA", "RETRY_THEN_REVIEW"),
        ("ECR_REPOSITORY_NOT_IMMUTABLE", "INFRA", "RETRY_THEN_REVIEW"),
        ("ECR_DIGEST_MISMATCH", "INFRA", "RETRY_THEN_REVIEW"),
        ("LOG_UPLOAD_FAILED", "INFRA", "RETRY_THEN_REVIEW"),
        ("EXTERNAL_COMMAND_FAILED", "BUILD", "BUILD_FAILED"),
        ("BUILD_DIGEST_MISSING", "BUILD", "BUILD_FAILED"),
        ("SOMETHING_NEW", "UNKNOWN", "REVIEW"),
    ],
)
def test_failure_codes_are_classified_with_a_next_action(code, failure_class, next_action):
    assert classify_failure(code) == (failure_class, next_action)


def test_an_external_command_failure_is_retried_once_with_the_same_attempt_id():
    result, calls = run(["EXTERNAL_COMMAND_FAILED", None])
    assert result.status == "SUCCEEDED" and result.attempts == 2
    assert [c[0].attempt_id for c in calls] == ["run1-b1", "run1-b1"]


def test_a_repeated_external_command_failure_ends_as_a_build_failure():
    result, calls = run(["EXTERNAL_COMMAND_FAILED"])
    assert len(calls) == 2 and {c[0].attempt_id for c in calls} == {"run1-b1"}
    assert (result.status, result.error_code) == ("FAILED", "EXTERNAL_COMMAND_FAILED")
    assert (result.failure_class, result.next_action) == ("BUILD", "BUILD_FAILED")


def test_other_failures_are_not_retried_inside_the_request():
    result, calls = run(["ECR_LOOKUP_FAILED"])
    assert len(calls) == 1 and result.next_action == "RETRY_THEN_REVIEW"


def test_the_module_is_called_with_the_archive_uri_version_and_the_right_context():
    _, calls = run([None])
    context, uri, version = calls[0]
    assert (uri, version) == (SOURCE.uri, "v1")
    assert (context.migration_id, context.resource_id, context.attempt_id) == (
        "mig-1",
        "api",
        "run1-b1",
    )
    assert (context.ecr_repository, context.platform, context.log_bucket) == (
        REPO,
        "linux/amd64",
        "cmp-build-logs",
    )


def test_success_requires_a_digest_pinned_uri_and_the_requested_archive():
    other = S3ArchiveRef(uri=f"s3://cmp-sources/sources/{'c' * 64}.tar.gz", sha256="c" * 64)
    wrong_source, _ = run([lambda c: outcome(c, source=other)])
    wrong_uri, _ = run([lambda c: outcome(c, uri=f"{REPO}@sha256:{'d' * 64}")])
    for bad in (wrong_source, wrong_uri):
        assert bad.status == "FAILED" and bad.error_code == "BUILD_RESULT_INCONSISTENT"
        assert bad.next_action == "REVIEW" and bad.image_uri is None


def test_the_result_becomes_build_evidence_v2_with_a_content_derived_result_ref():
    result, _ = run([None])
    evidence = to_build_evidence(result)
    BuildEvidenceV2.model_validate(evidence.model_dump())
    assert evidence.status == "SUCCEEDED" and evidence.platforms == ["linux/amd64"]
    assert evidence.image_uri == f"{REPO}@{DIGEST}" and evidence.image_digest == DIGEST
    assert evidence.source_ref == SOURCE and evidence.resource_id == "api"
    assert to_build_evidence(result).result_ref == evidence.result_ref
    failed, _ = run(["DOCKERFILE_MISSING"])
    failed_evidence = to_build_evidence(failed)
    assert failed_evidence.status == "FAILED" and failed_evidence.image_uri is None
    assert failed_evidence.platforms == []


def test_attempt_ids_follow_the_run_and_number_rule():
    assert make_attempt_id("run1", 3) == "run1-b3"
    for bad in [("run 1", 1), ("r" * 60, 1), ("run1", 0)]:
        with pytest.raises(ValueError):
            make_attempt_id(*bad)


def test_the_target_carries_no_credentials():
    with pytest.raises(ValidationError):
        BuildTarget(
            migration_id="m", ecr_repository=REPO, log_bucket="cmp-build-logs",
            platform="linux/amd64", aws_secret_access_key="x",
        )  # fmt: skip
    result, _ = run([None])
    dumped = result.model_dump_json()
    assert "secret" not in dumped.lower() and "password" not in dumped.lower()


def test_the_build_is_kept_alive_with_heartbeats_while_it_runs():
    beats = []
    result, _ = run([None], delay=0.25, heartbeat=lambda: beats.append(1), heartbeat_interval=0.05)
    assert result.status == "SUCCEEDED" and len(beats) >= 2


def test_saved_results_are_immutable():
    store = InMemoryBuildStore()
    first, _ = run([None], store=store)
    changed = first.model_copy(update={"image_digest": "sha256:" + "e" * 64})
    with pytest.raises(ValueError):
        store.save(changed)
    store.save(first)  # saving the identical result again is harmless
