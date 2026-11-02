"""Pure SHA-256 helpers for artifact bytes and the internal cmp-json-v1 format."""

import hashlib
import json

from cmp_contracts.artifact import ArtifactRef


def sha256_bytes(content: bytes) -> str:
    """Hash the exact stored file bytes. The caller owns reading and access checks."""
    return hashlib.sha256(content).hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    """cmp-json-v1: sorted string keys, UTF-8, no spaces, no floats or Unicode folding.

    Integers, booleans, null, strings, lists and string-keyed dictionaries only.
    Quantities requiring decimals must use explicitly defined string units.
    """

    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("Unsupported canonical JSON value")

    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def verify_artifact_bytes(reference: ArtifactRef, content: bytes) -> None:
    """Check bytes after the caller has authorized and resolved the immutable reference."""
    if len(content) != reference.size_bytes or sha256_bytes(content) != reference.sha256:
        raise ValueError("Artifact bytes do not match the approved reference")
