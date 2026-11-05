import hashlib
import stat

import pytest
from cmp_worker.artifact_store import ArtifactError, ArtifactRef, LocalArtifactStore


def test_content_is_stored_under_its_sha256_name_and_read_back(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    ref = store.put(b"hello", ref_id="requirements-m1", kind="requirements")
    digest = hashlib.sha256(b"hello").hexdigest()
    assert (ref.sha256, ref.size_bytes, ref.kind, ref.id, ref.version) == (
        digest,
        5,
        "requirements",
        "requirements-m1",
        "1",
    )
    assert (tmp_path / "store" / digest).read_bytes() == b"hello"
    assert store.get(ref) == b"hello"


def test_the_same_content_gives_the_same_ref_and_is_not_rewritten(tmp_path):
    store = LocalArtifactStore(tmp_path)
    first = store.put(b"same", ref_id="a", kind="x")
    path = tmp_path / first.sha256
    before = path.stat().st_mtime_ns
    second = store.put(b"same", ref_id="a", kind="x")
    assert first == second and path.stat().st_mtime_ns == before


def test_files_are_private_and_no_temporary_files_are_left_behind(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ref = store.put(b"data", ref_id="a", kind="x")
    assert stat.S_IMODE((tmp_path / ref.sha256).stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [ref.sha256]


def test_reading_verifies_the_digest(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ref = store.put(b"original", ref_id="a", kind="x")
    (tmp_path / ref.sha256).write_bytes(b"tampered")
    with pytest.raises(ArtifactError) as exc:
        store.get(ref)
    assert exc.value.code == "ARTIFACT_DIGEST_MISMATCH"


def test_missing_and_malformed_refs_are_refused(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ghost = ArtifactRef(id="a", version="1", kind="x", sha256="a" * 64, size_bytes=1)
    with pytest.raises(ArtifactError) as exc:
        store.get(ghost)
    assert exc.value.code == "ARTIFACT_UNAVAILABLE"
    with pytest.raises(Exception):  # noqa: B017 - the model rejects non-hash names
        ArtifactRef(id="a", version="1", kind="x", sha256="../etc/passwd", size_bytes=1)


def test_json_helpers_round_trip_models_and_dicts(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ref = store.put_json({"b": 1, "a": [1, 2]}, ref_id="doc", kind="assembly")
    assert store.get_json(ref) == {"a": [1, 2], "b": 1}
    again = store.put_json({"a": [1, 2], "b": 1}, ref_id="doc", kind="assembly")
    assert again.sha256 == ref.sha256  # key order does not change the hash
