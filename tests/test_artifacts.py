import pytest
from cmp_providers.artifacts import canonical_json_bytes, sha256_bytes, sha256_json


def test_sha256_matches_standard_known_vector():
    assert (
        sha256_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_canonical_json_is_order_independent_and_preserves_array_order():
    assert canonical_json_bytes({"z": [2, 1], "a": "한글"}) == '{"a":"한글","z":[2,1]}'.encode()
    assert sha256_json({"a": 1, "b": True}) == sha256_json({"b": True, "a": 1})
    assert sha256_json({"a": [1, 2]}) != sha256_json({"a": [2, 1]})


@pytest.mark.parametrize("value", [float("nan"), 1.0, {1: "value"}, b"secret", (1, 2)])
def test_canonical_json_rejects_ambiguous_or_non_json_values(value):
    with pytest.raises(ValueError, match="Unsupported canonical JSON value"):
        canonical_json_bytes(value)


def test_stored_artifact_bytes_must_match_reference_size_and_digest():
    from cmp_contracts.artifact import ArtifactRef
    from cmp_providers.artifacts import verify_artifact_bytes

    ref = ArtifactRef(
        id="file-001",
        version="1",
        kind="CHART",
        size_bytes=3,
        sha256="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    )
    verify_artifact_bytes(ref, b"abc")
    for content in (b"abd", b"abcd"):
        with pytest.raises(ValueError, match="Artifact bytes do not match the approved reference"):
            verify_artifact_bytes(ref, content)
