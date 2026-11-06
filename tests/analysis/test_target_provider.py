"""Ticket 31: the target provider travels with the analysis, so the Fixer can be offered a skill
for one target. `AnalysisInput` carries it in, `AnalysisReport` carries it on to the Sandbox loop
(the report reference is already in every `sbx_fix` payload, so no Workflow changes).

Seam: the CLI start input, the contracts and the pipeline step that writes the report.
"""

import json

import pytest
from cmp_contracts.analysis import AnalysisInput, AnalysisReport
from cmp_worker.artifact_store import ArtifactRef
from cmp_worker.start_input import build_start_input
from pipeline_support import MAIN_GO, git, make
from pydantic import ValidationError

PLANNING = {
    "target_selection": {
        "ref": {"id": "target-onprem", "version": "1"},
        "provider": "GCP",
        "location": "asia-northeast3",
        "access_ref": {"id": "conn", "version": "1"},
        "capabilities_ref": {"id": "profile-gcp", "version": "1"},
        "profile": "gcp-basic",
    },
    "target": {
        "provider": "GCP",
        "settings": {},
    },
}


def test_the_contracts_take_a_known_provider_and_default_to_none():
    assert AnalysisInput.model_fields["target_provider"].default is None
    assert AnalysisReport.model_fields["target_provider"].default is None
    assert AnalysisInput(migration_id="m", target_provider="GCP").target_provider == "GCP"
    with pytest.raises(ValidationError):
        AnalysisInput(migration_id="m", target_provider="AZURE")


def test_the_cli_start_input_takes_the_provider_from_the_planning_target():
    data = build_start_input(
        project_id="p",
        repository="r",
        commit="a" * 40,
        planning_json=json.dumps(PLANNING),
    )
    assert data["analysis"]["target_provider"] == "GCP"


def test_the_cli_start_input_without_a_planning_target_has_no_provider():
    data = build_start_input(project_id="p", repository="r", commit="a" * 40)
    assert data["analysis"]["target_provider"] is None


def report_of(repo, **input_fields):
    work = repo["work"]
    (work / "main.go").write_text(MAIN_GO)
    git(work, "commit", "-q", "--allow-empty", "-am", "x")
    git(work, "push", "-q", str(repo["url"]), "HEAD:refs/heads/main")
    sha = git(work, "rev-parse", "HEAD")
    pipeline = make(repo)
    inp = AnalysisInput(
        migration_id="mig-1",
        repository=repo["url"],
        requested_commit=sha,
        manifests_dir="k8s",
        **input_fields,
    ).model_dump(mode="json")
    source = pipeline.fetch_source(inp)
    scanned = pipeline.scan_code(inp, source)
    parsed = pipeline.scan_manifests(inp, source)
    infra = {"status": "NOT_COLLECTED", "reason": "AWS_SCAN_NOT_CONFIGURED"}
    built = pipeline.build_requirements(inp, source, scanned, parsed, infra)
    pre = pipeline.precheck(inp, source, scanned, parsed)
    report = pipeline.check_evidence(
        inp, source, built, infra, pre, {"status": "NOT_RUN", "rows_ref": None}
    )
    return pipeline.store.get_json(ArtifactRef.model_validate(report["report_ref"]))


def test_the_report_carries_the_provider_of_the_input(repo):
    assert report_of(repo, target_provider="GCP")["target_provider"] == "GCP"


def test_the_report_without_a_provider_says_none(repo):
    assert report_of(repo)["target_provider"] is None
