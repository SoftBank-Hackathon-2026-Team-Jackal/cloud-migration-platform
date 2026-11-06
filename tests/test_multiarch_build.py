"""Ticket 58: multi-architecture (linux/amd64 + linux/arm64) image builds.

The Build module builds each platform on the daemon's own `docker` driver (ticket 54 isolation),
then joins them into one manifest list (`docker buildx imagetools create`). The list digest is what
every later step (Sandbox, Planning, the Plan's node affinity) carries, with all its platforms.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cmp_contracts.provisioning_v2 import S3ArchiveRef
from cmp_contracts.verification import BuildAttempt
from cmp_worker.build_request import BuildTarget, InMemoryBuildStore, request_build
from cmp_worker.build_wiring import BuildSettings, ImageBuildAdapter, to_build_attempt
from cmp_worker.image_build import BuildContext, ImageBuilder, parse_platforms
from cmp_worker.plan_publish import deployment_request
from cmp_worker.provisioning_v2 import prepare_plan_v2

sys.path.insert(0, str(Path(__file__).parent))
from test_image_build import ECR, archive, mock_aws_docker, source_uri
from test_provisioning_v2 import _request

BOTH = ["linux/amd64", "linux/arm64"]


def _digest(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def multi_builder() -> ImageBuilder:
    return ImageBuilder(
        BuildContext(
            migration_id="migration-1",
            resource_id="api-main",
            attempt_id="attempt-1",
            ecr_repository=ECR,
            platforms=BOTH,
            log_bucket="log-bucket",
        )
    )


def mock_registry(monkeypatch, path, *, index_platforms=None, existing_tags=()):
    """`mock_aws_docker` plus a registry: per-tag digests, `imagetools create`/`inspect --raw`."""
    calls, logs = mock_aws_docker(monkeypatch, path)
    fallback = subprocess.run
    tags: dict[str, str] = {tag: _digest(tag) for tag in existing_tags}
    indexes: dict[str, list[str]] = {}

    def run(command, **kwargs):
        if command[:3] == ["aws", "ecr", "describe-images"]:
            calls.append(command)
            tag = command[command.index("--image-ids") + 1].removeprefix("imageTag=")
            if tag in tags:
                body = {"imageDetails": [{"imageDigest": tags[tag]}]}
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(body))
            raise subprocess.CalledProcessError(254, command, stderr="ImageNotFoundException")
        if command[:3] == ["docker", "buildx", "build"]:
            result = fallback(command, **kwargs)
            tag = command[command.index("--tag") + 1].rsplit(":", 1)[1]
            tags[tag] = _digest(tag)
            Path(command[command.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.digest": tags[tag]})
            )
            return result
        if command[:4] == ["docker", "buildx", "imagetools", "create"]:
            calls.append(command)
            assert kwargs["env"]["DOCKER_CONFIG"]
            tag = command[command.index("--tag") + 1].rsplit(":", 1)[1]
            tags[tag] = _digest(tag)
            indexes[tags[tag]] = index_platforms or BOTH
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            calls.append(command)
            digest = command[-1].rsplit("@", 1)[1]
            manifests = [
                {"digest": _digest(p), "platform": dict(zip(("os", "architecture"), p.split("/")))}
                for p in indexes[digest]
            ] + [
                {"digest": _digest("att"), "platform": {"os": "unknown", "architecture": "unknown"}}
            ]
            body = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": manifests}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(body))
        return fallback(command, **kwargs)

    monkeypatch.setattr("cmp_worker.image_build.subprocess.run", run)
    return calls, logs, tags


# -- 1. BUILD_PLATFORM: one value or a comma list ----------------------------------------------


def test_build_platform_accepts_one_value_or_a_comma_list():
    assert parse_platforms("linux/amd64") == ["linux/amd64"]
    assert parse_platforms("linux/amd64,linux/arm64") == BOTH
    assert parse_platforms(" linux/arm64 , linux/amd64 ") == ["linux/arm64", "linux/amd64"]


@pytest.mark.parametrize(
    "value", ["", "linux/386", "linux/amd64,linux/amd64", "linux/amd64,", "amd64", "linux/arm/v7"]
)
def test_other_build_platform_values_are_a_settings_error(value):
    with pytest.raises(ValueError, match="BUILD_PLATFORM_INVALID"):
        parse_platforms(value)
    with pytest.raises(ValueError, match="BUILD_PLATFORM_INVALID"):
        BuildSettings.from_values("src", ECR, "logs", value or " ", None)


def test_build_settings_keep_the_platform_list_in_order():
    settings = BuildSettings.from_values("src", ECR, "logs", "linux/amd64,linux/arm64", None)
    assert settings.platforms == BOTH
    assert settings.platform == "linux/amd64"  # the first entry: the Sandbox's architecture


def test_preflight_reports_the_platform_code(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent / "sandbox"))
    from test_preflight import check, settings

    config = settings(
        tmp_path,
        build_source_bucket="src",
        build_ecr_repository=ECR,
        build_log_bucket="logs",
        build_platform="linux/amd64,linux/s390x",
    )
    result = check(config)
    assert result["build"].status == "FAIL"
    assert result["build"].detail == "BUILD_PLATFORM_INVALID"


# -- 2. Build module: one build per platform, then a manifest list ----------------------------


def test_a_single_platform_build_is_unchanged(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    calls, _, _ = mock_registry(monkeypatch, path)
    single = ImageBuilder(multi_builder().context.model_copy(update={"platforms": ["linux/arm64"]}))
    result = single.build(source_uri(path))
    assert result.status == "SUCCEEDED" and result.platforms == ["linux/arm64"]
    (build,) = [c for c in calls if c[:3] == ["docker", "buildx", "build"]]
    assert build[build.index("--platform") + 1] == "linux/arm64"
    assert not [c for c in calls if c[:3] == ["docker", "buildx", "imagetools"]]


def test_two_platforms_are_built_separately_then_pushed_as_one_list(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    calls, logs, tags = mock_registry(monkeypatch, path)
    result = multi_builder().build(source_uri(path))
    assert result.status == "SUCCEEDED", result.error_code
    builds = [c for c in calls if c[:3] == ["docker", "buildx", "build"]]
    assert [b[b.index("--platform") + 1] for b in builds] == BOTH
    for build in builds:  # ticket 54 stays: the daemon's own builder on `docker0`
        assert build[build.index("--network") + 1] == "default" and "--builder" not in build
    arch_tags = [b[b.index("--tag") + 1].rsplit(":", 1)[1] for b in builds]
    (create,) = [c for c in calls if c[:4] == ["docker", "buildx", "imagetools", "create"]]
    main_tag = create[create.index("--tag") + 1].rsplit(":", 1)[1]
    assert arch_tags == [f"{main_tag}-amd64", f"{main_tag}-arm64"]
    assert create[-2:] == [f"{ECR}@{tags[t]}" for t in arch_tags]
    # The result is the list digest, not one platform's image.
    assert result.image_digest == tags[main_tag]
    assert result.image_uri == f"{ECR}@{tags[main_tag]}"
    assert result.platforms == BOTH
    assert logs[0]["platforms"] == BOTH


def test_a_list_missing_a_platform_is_not_a_successful_build(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    mock_registry(monkeypatch, path, index_platforms=["linux/amd64"])
    result = multi_builder().build(source_uri(path))
    assert result.status == "FAILED"
    assert result.error_code == "BUILD_INDEX_PLATFORM_MISMATCH"
    assert result.image_uri is None


def test_a_retry_reuses_platform_images_already_pushed(tmp_path, monkeypatch):
    """ECR tags are immutable: an image pushed before a failed list step is reused, not rebuilt."""
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    calls, _, _ = mock_registry(monkeypatch, path)
    first = multi_builder().build(source_uri(path))
    tags = {
        c[c.index("--tag") + 1].rsplit(":", 1)[1]
        for c in calls
        if c[:3] == ["docker", "buildx", "build"]
    }
    calls2, _, _ = mock_registry(monkeypatch, path, existing_tags=tags)
    second = multi_builder().build(source_uri(path))
    assert second.status == "SUCCEEDED" and second.image_digest == first.image_digest
    assert not [c for c in calls2 if c[:3] == ["docker", "buildx", "build"]]


def test_the_image_tag_depends_on_the_platform_list(tmp_path, monkeypatch):
    path = archive(tmp_path, {"Dockerfile": b"FROM scratch\n", "app.py": b"print('ok')"})
    calls, _, _ = mock_registry(monkeypatch, path)
    multi_builder().build(source_uri(path))
    ImageBuilder(multi_builder().context.model_copy(update={"platforms": ["linux/amd64"]})).build(
        source_uri(path)
    )
    created = [c for c in calls if c[:4] == ["docker", "buildx", "imagetools", "create"]]
    single = [c for c in calls if c[:3] == ["docker", "buildx", "build"]][-1]
    assert created[0][created[0].index("--tag") + 1] != single[single.index("--tag") + 1]


# -- 3. Build result → BuildAttempt → Planning → Plan -------------------------------------------


class _ListBuilder:
    def __init__(self, context):
        self.context = context

    def build(self, source_uri, *, source_version_id=None):
        from cmp_worker.image_build import BuildOutcome

        sha = Path(source_uri).name.removesuffix(".tar.gz")
        digest = _digest("index")
        return BuildOutcome(
            migration_id=self.context.migration_id,
            resource_id=self.context.resource_id,
            attempt_id=self.context.attempt_id,
            status="SUCCEEDED",
            source_ref=S3ArchiveRef(uri=source_uri, version_id=source_version_id, sha256=sha),
            platforms=list(self.context.platforms),
            image_digest=digest,
            image_uri=f"{self.context.ecr_repository}@{digest}",
        )


def test_the_build_result_image_and_attempt_carry_every_platform(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    class Archives:
        def exists(self, bucket, key):
            return False

        def put(self, bucket, key, data):
            return "v1"

    settings = BuildSettings.from_values("src", ECR, "logs", "linux/amd64,linux/arm64", None)
    adapter = ImageBuildAdapter(
        settings, archive_store=Archives(), build_store=InMemoryBuildStore(),
        builder_factory=_ListBuilder,
    )  # fmt: skip
    image, result = adapter.build_with_evidence(
        "app", {"workspace": str(tmp_path)}, migration_id="mig-1", run_id="run1", attempt=1
    )
    assert image.platforms == BOTH  # the Sandbox chart allows both; the amd64 node schedules
    assert result.platforms == BOTH
    attempt = to_build_attempt(result, migration_id="mig-1")
    assert attempt.platforms == BOTH
    assert attempt.platform == "linux/amd64"  # what the Sandbox ran


def test_a_build_reused_from_the_store_keeps_its_platforms():
    sha = "a" * 64
    source = S3ArchiveRef(uri=f"s3://src/sources/{sha}.tar.gz", sha256=sha)
    target = BuildTarget(migration_id="m", ecr_repository=ECR, log_bucket="logs", platforms=BOTH)
    store = InMemoryBuildStore()
    first = request_build(
        run_id="run1", attempt=1, resource_id="app", source=source, target=target,
        store=store, builder_factory=_ListBuilder,
    )  # fmt: skip
    again = request_build(
        run_id="run1", attempt=2, resource_id="app", source=source, target=target,
        store=store, builder_factory=_ListBuilder,
    )  # fmt: skip
    assert again.reused and again.platforms == first.platforms == BOTH
    amd = target.model_copy(update={"platforms": ["linux/amd64"]})
    assert store.find("app", sha, amd.platforms) is None  # a different list is a different image


def test_a_build_attempt_recorded_before_ticket_58_still_reads():
    sha = "a" * 64
    old = {
        "migration_id": "m", "resource_id": "app", "attempt_id": "run1-b1",
        "result_ref": {"id": "build-app", "version": "1"}, "status": "SUCCEEDED",
        "source_ref": {"uri": f"s3://src/sources/{sha}.tar.gz", "version_id": None,
                       "sha256": sha},
        "platform": "linux/amd64", "image_uri": f"{ECR}@{_digest('x')}",
        "image_digest": _digest("x"), "log": None, "error_code": None,
    }  # fmt: skip
    attempt = BuildAttempt.model_validate(old)
    assert attempt.platforms == ["linux/amd64"]
    with pytest.raises(ValueError):
        BuildAttempt.model_validate({**old, "platforms": ["linux/arm64"]})  # platform not in list


def test_planning_keeps_the_build_platforms_and_the_plan_allows_both():
    payload, content = _request()
    payload["builds"][0]["platforms"] = BOTH
    spec = prepare_plan_v2(payload, requirements_bytes=content).spec
    assert spec.images[0].platform == "linux/amd64"  # validated in the Sandbox
    assert spec.images[0].platforms == BOTH

    class Target:
        namespace = "demo"
        storage_class = "local-path"
        database_image = "docker.io/library/postgres@sha256:" + "c" * 64

    request = deployment_request(spec, Target())
    images = {item["kind"]: item["image"] for item in request["resources"]}
    assert images["APPLICATION"]["platforms"] == BOTH
    assert images["POSTGRESQL"]["platforms"] == BOTH  # official postgres is multi-arch


def test_a_sandbox_platform_outside_the_build_list_is_still_refused():
    payload, content = _request()
    payload["builds"][0]["platforms"] = ["linux/arm64"]
    from cmp_contracts.provisioning import ProvisioningInputError

    with pytest.raises(ProvisioningInputError) as caught:
        prepare_plan_v2(payload, requirements_bytes=content)
    assert {issue.code for issue in caught.value.issues} == {"PLATFORM_MISMATCH"}


def test_planning_handoff_records_the_sandbox_platform_and_the_build_list():
    from cmp_contracts.verification import project_planning  # noqa: F401 - the projection below

    sys.path.insert(0, str(Path(__file__).parent / "sandbox"))
    from cmp_worker.planning_handoff import assemble_registration
    from test_handoff import evidence

    args, _ = evidence()
    build = args["builds"][0].model_copy(update={"platforms": BOTH})
    request = assemble_registration(**{**args, "builds": [build]}).planning_request
    assert request.builds[0].platforms == BOTH
    assert request.sandbox.validated_workloads[0].platform == "linux/amd64"


@pytest.mark.parametrize(
    "platforms,mismatch", [(BOTH, False), (["linux/amd64"], True), (["linux/arm64"], False)]
)
def test_arm64_on_prem_target_accepts_a_list_with_arm64(platforms, mismatch):
    from cmp_contracts.provisioning import ProvisioningSpec
    from cmp_providers.provisioning import validate_target
    from cmp_worker.provisioning import prepare_plan

    fixture = Path(__file__).parent / "fixtures" / "provisioning" / "on_prem.json"
    data = prepare_plan(json.loads(fixture.read_text())).spec.model_dump(mode="json")
    data["target"]["architecture"] = "linux/arm64"
    data["image"]["platforms"] = platforms
    codes = {issue.code for issue in validate_target(ProvisioningSpec.model_validate(data))}
    assert ("ARCHITECTURE_MISMATCH" in codes) is mismatch


# -- 5. Demo server preparation (scripts only; the server is changed by the release) -----------

DEMO = Path(__file__).resolve().parents[1] / "infra/demo"


def test_binfmt_installer_is_pinned_offline_and_reapplied_with_docker():
    text = (DEMO / "build-binfmt.sh").read_text()
    assert "image=docker.io/tonistiigi/binfmt@sha256:" in text  # digest-pinned, not a tag
    assert 'docker run --rm --privileged --network none "$image" --install "$arch"' in text
    assert "x86_64 | amd64) arch=arm64 entry=qemu-aarch64" in text
    unit = text.split("<<UNIT", 1)[1].split("\nUNIT", 1)[0]
    assert "After=docker.service" in unit and "BindsTo=docker.service" in unit
    assert "WantedBy=docker.service" in unit and "ExecStart=$target apply" in unit
    assert '"$target" check' in text
    # Ticket 54 stays intact: nothing here touches iptables or selects another builder.
    assert "iptables" not in text and "buildx create" not in text


def test_start_installs_binfmt_after_the_imds_guard_and_before_the_worker():
    lines = (DEMO / "start.sh").read_text().splitlines()
    guard = lines.index("sh infra/demo/build-imds-guard.sh install")
    binfmt = lines.index("  sh infra/demo/build-binfmt.sh install")
    worker = next(i for i, line in enumerate(lines) if "up -d --build api worker" in line)
    assert guard < binfmt < worker
    assert "if grep -q '^BUILD_PLATFORM=.*linux/arm64' /opt/cmp/.env; then" in lines


def test_runtime_check_reports_builder_platforms_and_the_deploy_hint_lists_both():
    check = (DEMO / "runtime-check.sh").read_text()
    for name in (
        "build_binfmt_service=", "builder_$arch=", "build_platforms_supported=",
        "build_platform_first_is_host=",
    ):  # fmt: skip
        assert f'echo "{name}' in check
    assert "BUILD_PLATFORM=linux/amd64,linux/arm64" in (DEMO / "deploy-build-stack.sh").read_text()
