"""Ticket 34: safeguards around a red-team run, and the contract of what a run reports.

Part 1 (this block): the Finding contract and the masking of evidence before anything is stored.
Seams: pure models, and `store_evidence` on the local artifact store. Nothing here talks to a
cluster. The preflight (part 2) and the run wrapper (part 3) are tested further down.
"""

import base64
import json
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.redteam_safeguards import (
    Finding,
    Intensity,
    Severity,
    make_finding,
    store_evidence,
    summarize,
)
from pydantic import ValidationError

DIGEST = "sha256:" + "a" * 64
SUITE = "b" * 64
SECRET = "Zx9-live-key-4471"  # an app's real env value, as the Sandbox Secret holds it
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def finding(**changes):
    fields = {
        "finding_id": "f-1",
        "category": "input-validation",
        "severity": Severity.HIGH,
        "endpoint": "/orders",
        "summary": "A numeric field accepts text.",
        "repro_summary": "POST /orders with a text value in `quantity`.",
        "evidence_ref": None,
        "image_digest": DIGEST,
        "suite_sha256": SUITE,
        "intensity": Intensity.PASSIVE,
        "executed_at": NOW,
    }
    return Finding(**(fields | changes))


# --- the Finding contract -----------------------------------------------------------------------


def test_a_finding_always_names_the_image_and_the_suite_it_was_found_against():
    assert finding().image_digest == DIGEST and finding().suite_sha256 == SUITE
    for field in ("image_digest", "suite_sha256"):
        with pytest.raises(ValidationError):
            Finding(**{k: v for k, v in finding().model_dump().items() if k != field})
    for bad in ("", "latest", "sha256:abc", "sha256:" + "G" * 64):
        with pytest.raises(ValidationError):
            finding(image_digest=bad)
    with pytest.raises(ValidationError):
        finding(suite_sha256="not-a-hash")


def test_a_finding_points_at_a_path_inside_the_app_and_never_at_a_host():
    assert finding(endpoint="/api/v1/items/{id}").endpoint == "/api/v1/items/{id}"
    for bad in ("http://example.com/x", "example.com/x", "//example.com/x", "10.0.0.1", ""):
        with pytest.raises(ValidationError):
            finding(endpoint=bad)


def test_the_free_text_fields_are_short():
    with pytest.raises(ValidationError):
        finding(summary="x" * 301)
    with pytest.raises(ValidationError):
        finding(repro_summary="x" * 501)


def test_a_finding_survives_a_json_round_trip_and_rejects_unknown_fields():
    original = finding()
    assert Finding.model_validate_json(original.model_dump_json()) == original
    with pytest.raises(ValidationError):
        Finding.model_validate({**json.loads(original.model_dump_json()), "raw_response": "..."})


def test_make_finding_masks_secret_looking_text_in_the_summaries():
    made = make_finding(
        finding_id="f-2",
        category="information-exposure",
        severity=Severity.CRITICAL,
        endpoint="/debug",
        summary="The page prints AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP.",
        repro_summary="GET /debug returns password=hunter2hunter2 in the body.",
        known_secrets=["hunter2hunter2"],
        image_digest=DIGEST,
        suite_sha256=SUITE,
        intensity=Intensity.ACTIVE,
        executed_at=NOW,
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in made.summary
    assert "hunter2hunter2" not in made.repro_summary
    assert made.evidence_ref is None


# --- evidence -----------------------------------------------------------------------------------


def stored_text(store, result):
    return store.get(result.ref).decode()


def test_evidence_is_stored_without_the_apps_secret_in_any_common_form(tmp_path):
    store = LocalArtifactStore(tmp_path)
    forms = [
        SECRET,
        quote(SECRET, safe=""),
        base64.b64encode(SECRET.encode()).decode(),
        base64.urlsafe_b64encode(SECRET.encode()).decode(),
    ]
    evidence = "\n".join(f"line {i}: {form}" for i, form in enumerate(forms))
    result = store_evidence(store, finding_id="f-1", evidence=evidence, known_secrets=[SECRET])
    assert result.status == "STORED" and result.ref is not None
    text = stored_text(store, result)
    for form in forms:
        assert form not in text
    assert "line 0:" in text  # the rest of the evidence is kept


def test_evidence_with_a_known_key_pattern_is_masked_even_without_a_known_value(tmp_path):
    store = LocalArtifactStore(tmp_path)
    result = store_evidence(
        store, finding_id="f-1", evidence="id=AKIAABCDEFGHIJKLMNOP", known_secrets=[]
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in stored_text(store, result)


def test_evidence_that_still_shows_the_secret_in_a_form_we_cannot_redact_is_not_stored(tmp_path):
    store = LocalArtifactStore(tmp_path)
    result = store_evidence(
        store, finding_id="f-1", evidence=f"shouted: {SECRET.upper()}", known_secrets=[SECRET]
    )
    assert result.status == "SECRET_IN_EVIDENCE" and result.ref is None
    assert list(tmp_path.rglob("*")) == [] or all(p.is_dir() for p in tmp_path.rglob("*"))


def test_the_evidence_reference_is_content_addressed_and_typed(tmp_path):
    store = LocalArtifactStore(tmp_path)
    one = store_evidence(store, finding_id="f-1", evidence="same", known_secrets=[])
    two = store_evidence(store, finding_id="f-1", evidence="same", known_secrets=[])
    assert one.ref == two.ref and one.ref.kind == "redteam-evidence"
    assert one.ref.id == "redteam-evidence-f-1"


def test_very_short_values_are_not_treated_as_secrets(tmp_path):
    # "true" or "8080" are env values too, but redacting them would destroy every response.
    store = LocalArtifactStore(tmp_path)
    result = store_evidence(
        store, finding_id="f-1", evidence="debug=true port=8080", known_secrets=["true", "8080"]
    )
    assert stored_text(store, result) == "debug=true port=8080"


# --- what the workflow history gets --------------------------------------------------------------


def test_the_summary_for_the_workflow_history_holds_counts_and_ids_only():
    findings = [
        finding(finding_id="f-1", severity=Severity.LOW),
        finding(finding_id="f-2", severity=Severity.CRITICAL, summary="SECRET TEXT HERE"),
        finding(finding_id="f-3", severity=Severity.LOW),
    ]
    summary = summarize(findings)
    assert summary.count == 3 and summary.max_severity == Severity.CRITICAL
    assert summary.by_severity == {"LOW": 2, "CRITICAL": 1}
    assert summary.finding_ids == ["f-1", "f-2", "f-3"]
    dumped = summary.model_dump_json()
    assert "SECRET TEXT HERE" not in dumped and "/orders" not in dumped


def test_an_empty_run_summarizes_to_nothing():
    summary = summarize([])
    assert summary.count == 0 and summary.max_severity is None and summary.finding_ids == []
