"""Ticket 24: the exported event schema matches the web contract copy, and names translate."""

import json
from pathlib import Path

import pytest
from cmp_contracts.events import DATA_MODELS, event_json_schema, to_web, validate_data

WEB_SCHEMA = Path(__file__).resolve().parents[1] / "docs/contracts/events.schema.json"
IGNORED = {"description", "title", "default", "$schema", "$id"}


def canon(node: dict, defs: dict):
    """Semantic normal form: ignores annotations and Pydantic/hand-written layout differences."""
    if "$ref" in node:
        return canon(defs[node["$ref"].split("/")[-1]], defs)
    for key in ("anyOf", "oneOf"):
        if key in node:
            branches = node[key]
            nullable = any(b == {"type": "null"} for b in branches)
            rest = [b for b in branches if b != {"type": "null"}]
            if all(set(b) == {"type"} for b in branches):  # plain type union == type list
                return {"types": frozenset(b["type"] for b in branches)}
            if len(rest) == 1:
                out = dict(canon(rest[0], defs))
                if nullable:
                    if "enum" in out:
                        out["enum"] = out["enum"] | {None}
                    else:
                        out["types"] = out.get("types", frozenset()) | {"null"}
                return out
            return {
                key: sorted(json.dumps(canon(b, defs), sort_keys=True, default=_set) for b in rest)
            }
    out: dict = {}
    types = node.get("type")
    types = frozenset([types] if isinstance(types, str) else types or [])
    if "const" in node:
        out["enum"] = frozenset([node["const"]])
    elif "enum" in node:
        out["enum"] = frozenset(node["enum"])
    if "enum" in out:
        if "null" in types:
            out["enum"] = out["enum"] | {None}
    elif types:
        out["types"] = types
    for key in ("minimum", "maximum", "exclusiveMinimum", "pattern", "minItems", "minLength"):
        if key in node:
            out[key] = node[key]
    if "properties" in node or types == {"object"}:
        out["properties"] = {k: canon(v, defs) for k, v in node.get("properties", {}).items()}
        out["required"] = frozenset(node.get("required", []))
        extra = node.get("additionalProperties", True)
        out["additional"] = canon(extra, defs) if isinstance(extra, dict) else extra
    if "items" in node:
        out["items"] = canon(node["items"], defs)
    unknown = (
        set(node)
        - IGNORED
        - {
            "type",
            "const",
            "enum",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "pattern",
            "minItems",
            "minLength",
        }
    )
    assert not unknown, unknown
    return out


def _set(value):
    return sorted(value, key=str)


@pytest.fixture(scope="module")
def schemas():
    return json.loads(WEB_SCHEMA.read_text()), event_json_schema()


def test_event_types_match_web(schemas):
    web, ours = schemas
    assert [r["$ref"] for r in ours["oneOf"]] and {r["$ref"] for r in ours["oneOf"]} == {
        r["$ref"] for r in web["oneOf"]
    }
    assert set(DATA_MODELS) | {"snapshot"} == {
        r["$ref"].removeprefix("#/$defs/event:") for r in web["oneOf"]
    }


@pytest.mark.parametrize(
    "key",
    [k for k in json.loads(WEB_SCHEMA.read_text())["$defs"] if not k.startswith("model:")],
)
def test_each_definition_matches_web(schemas, key):
    web, ours = schemas
    assert key in ours["$defs"], key
    assert canon(ours["$defs"][key], ours["$defs"]) == canon(web["$defs"][key], web["$defs"])


def test_backend_names_are_translated():
    assert to_web("data.phase", {"phase": "catching_up"}) == {"phase": "catchup"}
    assert to_web("data.phase", {"phase": "copying"}) == {"phase": "copy"}
    readiness = validate_data(
        "data.readiness",
        {"longest_xact_ms": 12, "lock_waiters": 1, "commits_per_sec": 3.5, "ready": False},
    )
    assert readiness == {
        "longest_tx_ms": 12,
        "lock_waits": 1,
        "commits_per_sec": 3.5,
        "ready": False,
    }
    check = to_web(
        "data.preflight",
        {
            "checks": [
                {"name": "check_route"},
                {
                    "name": "probe_large_transfer",
                    "bytes_received": 4,
                    "bytes_expected": 8,
                    "seconds": 1.5,
                },
            ]
        },
    )
    assert check["checks"][0]["name"] == "route"
    assert check["checks"][1] == {
        "name": "large_transfer",
        "bytes_done": 4,
        "bytes_total": 8,
        "elapsed_ms": 1500.0,
    }
    built = validate_data(
        "build.result",
        {
            "attempt": 1,
            "max_attempts": 3,
            "status": "SUCCEEDED",
            "image_digest": "sha256:" + "a" * 64,
            "image_uri": "r/app",
            "reused": False,
            "error_code": None,
        },
    )
    assert built["digest"] == "sha256:" + "a" * 64


def test_optional_fields_are_omitted_and_required_nulls_kept():
    lsn = validate_data(
        "data.lsn", {"source_lsn": "0/1", "target_lsn": None, "lag_bytes": 0, "lag_ms": None}
    )
    assert lsn == {"source_lsn": "0/1", "target_lsn": None, "lag_bytes": 0, "lag_ms": None}
    resource = validate_data(
        "provision.resource", {"id": "r1", "key": "vpc", "state": "running", "progress": 10}
    )
    assert "name" not in resource and "detail" not in resource


def test_unknown_type_and_extra_fields_are_rejected():
    with pytest.raises(ValueError):
        validate_data("plan.unknown", {})
    with pytest.raises(ValueError):
        validate_data("provision.check", {"name": "pods_ready", "state": "done", "extra": 1})


def test_plan_ready_scope_is_optional_and_limited_to_approval_scopes():
    old = {"plan_id": "plan-1", "revision": 1}
    assert validate_data("plan.ready", old) == old  # events before the field still validate
    assert validate_data("plan.ready", old | {"scope": "WORKLOADS"})["scope"] == "WORKLOADS"
    with pytest.raises(ValueError):
        validate_data("plan.ready", old | {"scope": "workloads"})
