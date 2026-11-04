import os
import stat
import tarfile
from pathlib import Path

import pytest
from cmp_contracts.provisioning_v2 import S3ArchiveRef
from cmp_worker import image_build
from cmp_worker.source_archive import (
    AwsCliArchiveStore,
    SourceArchiveError,
    build_source_refs,
    package_source,
)

BUCKET = "cmp-test-sources"


class FakeStore:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.uploads = []

    def exists(self, bucket, key):
        return (bucket, key) in self.existing

    def put(self, bucket, key, data):
        self.uploads.append((bucket, key, data))
        self.existing.add((bucket, key))
        return "v1"


def write(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return root


@pytest.fixture
def app(tmp_path):
    return write(
        tmp_path / "app",
        {
            "Dockerfile": "FROM scratch\nCOPY main.go .\n",
            "main.go": "package main\n",
            "pkg/a.go": "package pkg\n",
        },
    )


def pack(workspace, store=None, **kw):
    kw.setdefault("resource_id", "api")
    return package_source(workspace, store=store or FakeStore(), bucket=BUCKET, **kw)


def members(data: bytes, tmp_path: Path):
    archive = tmp_path / "x.tar.gz"
    archive.write_bytes(data)
    with tarfile.open(archive, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read().decode() for m in tar if m.isfile()}


def uploaded(store):
    return store.uploads[0][2]


def test_same_input_gives_the_same_hash_regardless_of_mtime_and_creation_order(tmp_path):
    files = {"Dockerfile": "FROM scratch\n", "b.go": "b\n", "a/c.go": "c\n"}
    first = write(tmp_path / "one", files)
    second = tmp_path / "two"
    write(second, dict(reversed(list(files.items()))))
    for path in second.rglob("*"):
        if path.is_file():
            os.utime(path, (1_000_000, 1_000_000))
    a, b = pack(first), pack(second)
    assert a.ref.sha256 == b.ref.sha256
    assert pack(first).ref.sha256 == a.ref.sha256


def test_sources_are_archived_verbatim_not_masked(app, tmp_path):
    (app / "main.go").write_text('var key = "AKIAABCDEFGHIJKLMNOP"\n')
    store = FakeStore()
    pack(app, store)
    assert members(uploaded(store), tmp_path)["main.go"] == 'var key = "AKIAABCDEFGHIJKLMNOP"\n'


def test_secrets_vcs_data_links_and_special_files_never_enter_the_archive(app, tmp_path):
    write(
        app,
        {
            ".git/config": "x",
            ".env": "A=1",
            ".env.local": "A=2",
            ".aws/credentials": "x",
            "id.pem": "k",
            "deep/server.key": "k",
        },
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (app / "link.txt").symlink_to(outside)
    (app / "linkdir").symlink_to(tmp_path)
    os.mkfifo(app / "pipe")
    store = FakeStore()
    pack(app, store)
    assert set(members(uploaded(store), tmp_path)) == {"Dockerfile", "main.go", "pkg/a.go"}


def test_the_archive_passes_the_b01_archive_check_and_matches_its_ref_contract(app, tmp_path):
    result = pack(app)
    assert S3ArchiveRef.model_validate(result.ref.model_dump()) == result.ref
    store = FakeStore()
    pack(app, store)
    archive = tmp_path / f"{result.ref.sha256}.tar.gz"
    archive.write_bytes(uploaded(store))
    context = tmp_path / "ctx"
    context.mkdir()
    image_build._extract(archive, context)  # raises BuildFailure on any unsafe entry
    assert (context / "Dockerfile").is_file()


def test_an_existing_object_is_not_uploaded_again_and_a_new_one_is(app):
    store = FakeStore()
    first = pack(app, store)
    assert first.uploaded is True and len(store.uploads) == 1
    key = store.uploads[0][1]
    assert key == f"sources/{first.ref.sha256}.tar.gz"
    assert first.ref.uri == f"s3://{BUCKET}/{key}" and first.ref.version_id == "v1"
    again = pack(app, store)
    assert again.uploaded is False and len(store.uploads) == 1
    assert again.ref.sha256 == first.ref.sha256 and again.ref.version_id is None


def test_dockerignore_rules_apply_but_the_dockerfile_always_stays(app, tmp_path):
    write(
        app,
        {
            ".dockerignore": "*.log\ntmp/\n**/*.test\n!keep.log\nDockerfile\n# comment\n",
            "run.log": "x", "keep.log": "y", "tmp/x.go": "x", "pkg/a.test": "x", "pkg/b.go": "b",
        },
    )  # fmt: skip
    store = FakeStore()
    pack(app, store)
    names = set(members(uploaded(store), tmp_path))
    assert names == {"Dockerfile", ".dockerignore", "main.go", "keep.log", "pkg/a.go", "pkg/b.go"}


def test_a_nested_build_context_becomes_the_archive_root(tmp_path):
    root = write(
        tmp_path / "mono",
        {"svc/Dockerfile": "FROM scratch\n", "svc/app.py": "x", "other/x.py": "x"},
    )
    store = FakeStore()
    pack(root, store, context="svc")
    assert set(members(uploaded(store), tmp_path)) == {"Dockerfile", "app.py"}


def test_a_differently_named_dockerfile_is_placed_at_the_root_as_dockerfile(tmp_path):
    root = write(tmp_path / "w", {"Dockerfile.prod": "FROM prod\n", "main.go": "x"})
    store = FakeStore()
    pack(root, store, dockerfile="Dockerfile.prod")
    assert members(uploaded(store), tmp_path)["Dockerfile"] == "FROM prod\n"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"context": ".."}, "CONTEXT_OUTSIDE_WORKSPACE"),
        ({"context": "nope"}, "CONTEXT_MISSING"),
        ({"dockerfile": "Missing"}, "DOCKERFILE_MISSING"),
        ({"resource_id": "bad id"}, "INVALID_RESOURCE_ID"),
    ],
)
def test_bad_inputs_are_refused_before_anything_is_uploaded(app, kwargs, code):
    store = FakeStore()
    with pytest.raises(SourceArchiveError) as exc:
        pack(app, store, **kwargs)
    assert exc.value.code == code and store.uploads == []


def test_a_dockerfile_that_is_a_symlink_is_refused(tmp_path):
    root = write(tmp_path / "w", {"real": "FROM scratch\n"})
    (root / "Dockerfile").symlink_to(root / "real")
    with pytest.raises(SourceArchiveError) as exc:
        pack(root)
    assert exc.value.code == "DOCKERFILE_MISSING"


def test_archives_over_the_limits_are_refused(app, monkeypatch):
    monkeypatch.setattr("cmp_worker.source_archive.MAX_FILES", 2)
    with pytest.raises(SourceArchiveError) as exc:
        pack(app)
    assert exc.value.code == "ARCHIVE_TOO_LARGE"


def test_source_refs_are_keyed_by_resource_id_and_ids_cannot_repeat(app, tmp_path):
    other = write(tmp_path / "db", {"Dockerfile": "FROM postgres\n"})
    a, b = pack(app, resource_id="api"), pack(other, resource_id="worker")
    refs = build_source_refs([a, b])
    assert set(refs) == {"api", "worker"} and refs["api"] == a.ref
    with pytest.raises(SourceArchiveError):
        build_source_refs([a, a])


def fake_aws(tmp_path, script):
    path = tmp_path / "aws"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_cli_store_reports_missing_objects_and_raises_on_other_errors(tmp_path):
    log = tmp_path / "calls"
    missing = AwsCliArchiveStore(
        aws_bin=fake_aws(
            tmp_path,
            f'echo "$@" >> {log}\necho "An error occurred (404) when calling HeadObject: Not Found" >&2\nexit 254\n',
        )
    )
    assert missing.exists(BUCKET, "sources/x.tar.gz") is False
    assert "head-object --bucket cmp-test-sources --key sources/x.tar.gz" in log.read_text()
    broken = AwsCliArchiveStore(aws_bin=fake_aws(tmp_path, 'echo "AccessDenied" >&2\nexit 254\n'))
    with pytest.raises(SourceArchiveError) as exc:
        broken.exists(BUCKET, "k")
    assert exc.value.code == "STORE_ERROR" and "AccessDenied" not in str(exc.value)
    present = AwsCliArchiveStore(aws_bin=fake_aws(tmp_path, "echo '{}'\n"))
    assert present.exists(BUCKET, "k") is True


def test_cli_store_uploads_with_a_body_file_and_returns_the_version_id(tmp_path):
    log = tmp_path / "calls"
    store = AwsCliArchiveStore(
        aws_bin=fake_aws(tmp_path, f'echo "$@" >> {log}\necho \'{{"VersionId": "abc123"}}\'\n')
    )
    assert store.put(BUCKET, "sources/h.tar.gz", b"data") == "abc123"
    call = log.read_text()
    assert "put-object --bucket cmp-test-sources --key sources/h.tar.gz --body" in call
    assert "data" not in call
