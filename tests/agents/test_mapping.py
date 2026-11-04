import pytest
from cmp_agents.mapping import (
    apply_dispositions,
    check_evidence,
    compute_treatment,
    stabilize_row_ids,
)
from cmp_contracts.analysis import Evidence, MappingRow

CATALOG = {"container-runtime", "postgres", "object-storage"}


def row(id="r1", capability="postgres", usage="pgx driver", evidence=(), **kw):
    fields = {
        "id": id,
        "capability": capability,
        "usage": usage,
        "grade": "DIRECT",
        "required_proposed": True,
        "required_reason": "core data store",
        "required": None,
        "disposition": "MIGRATE",
        "loses": [],
        "treatment": None,
        "status": "UNVERIFIED",
        "evidence": list(evidence),
    }
    fields.update(kw)
    return MappingRow(**fields)


def code(locator, verified=False):
    return Evidence(kind="CODE", locator=locator, verified=verified)


@pytest.mark.parametrize(
    ("grade", "required", "expected"),
    [
        ("DIRECT", True, "PROCEED"),
        ("DIRECT", False, "PROCEED"),
        ("API_DIFF", True, "PROCEED"),
        ("API_DIFF", False, "PROCEED"),
        ("PARTIAL", True, "BLOCK"),
        ("PARTIAL", False, "APPROVAL"),
        ("NONE", True, "BLOCK"),
        ("NONE", False, "APPROVAL"),
    ],
)
def test_treatment_follows_grade_and_required_table(grade, required, expected):
    assert compute_treatment(grade, required) == expected


def test_partial_required_proceeds_only_when_an_alternative_is_proven():
    assert compute_treatment("PARTIAL", True, alternative_proven=True) == "PROCEED"
    assert compute_treatment("NONE", True, alternative_proven=True) == "BLOCK"


def test_treatment_is_undecided_until_required_is_confirmed():
    assert compute_treatment("NONE", None) is None


def test_same_usage_keeps_its_previous_id_across_analysis_versions():
    previous = [row(id="r1"), row(id="r2", capability="object-storage", usage="presigned urls")]
    fresh = [
        row(id="x9"),
        row(id="x8", capability="object-storage", usage="presigned urls"),
        row(id="x7", capability="postgres", usage="advisory lock"),
    ]
    result = stabilize_row_ids(fresh, previous)
    assert [r.id for r in result[:2]] == ["r1", "r2"]
    assert result[2].id not in {"r1", "r2"}
    assert len({r.id for r in result}) == 3


def test_ids_stay_unique_when_new_ids_collide_or_usages_repeat():
    previous = [row(id="r1")]
    fresh = [row(id="r1", usage="brand new"), row(id="a"), row(id="b")]
    result = stabilize_row_ids(fresh, previous)
    assert len({r.id for r in result}) == 3
    assert result[1].id == "r1"  # the row with the previous usage wins the old id
    assert result[0].id != "r1" and result[2].id != "r1"


def test_object_storage_is_kept_in_the_first_cycle():
    rows = [row(capability="object-storage", usage="uploads"), row()]
    result = apply_dispositions(rows)
    assert [r.disposition for r in result] == ["KEEP", "MIGRATE"]


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "main.go").write_text("line1\nline2\nline3\n")
    return tmp_path


def check(rows, workspace, **kw):
    kw.setdefault("manifest_locators", {"app.yaml#Deployment/api"})
    kw.setdefault("catalog_capabilities", CATALOG)
    return check_evidence(rows, workspace=workspace, **kw)


def test_existing_code_evidence_is_verified_and_the_row_is_checked(workspace):
    result = check([row(evidence=[code("main.go:3")])], workspace)[0]
    assert result.evidence[0].verified is True
    assert result.status == "EVIDENCE_CHECKED"


@pytest.mark.parametrize(
    "locator", ["main.go:4", "missing.go:1", "../outside.go:1", "main.go", "main.go:0"]
)
def test_missing_or_out_of_range_evidence_leaves_the_row_unverified(workspace, locator):
    result = check([row(evidence=[code(locator)])], workspace)[0]
    assert result.evidence[0].verified is False
    assert result.status == "UNVERIFIED"


def test_manifest_evidence_is_verified_against_scanned_locators(workspace):
    good = Evidence(kind="MANIFEST", locator="app.yaml#Deployment/api", verified=False)
    bad = Evidence(kind="MANIFEST", locator="app.yaml#Deployment/ghost", verified=False)
    result = check([row(evidence=[good]), row(id="r2", evidence=[bad])], workspace)
    assert [r.status for r in result] == ["EVIDENCE_CHECKED", "UNVERIFIED"]


def test_one_missing_evidence_keeps_the_row_unverified(workspace):
    result = check([row(evidence=[code("main.go:1"), code("main.go:9")])], workspace)[0]
    assert [e.verified for e in result.evidence] == [True, False]
    assert result.status == "UNVERIFIED"


def test_rows_without_evidence_stay_unverified(workspace):
    assert check([row()], workspace)[0].status == "UNVERIFIED"


def test_capability_outside_the_catalog_never_rises_above_unverified(workspace):
    result = check([row(capability="quantum-db", evidence=[code("main.go:1")])], workspace)[0]
    assert result.evidence[0].verified is True
    assert result.status == "UNVERIFIED"


def test_cloud_side_evidence_stays_unverified_without_a_known_resource(workspace):
    cloud = Evidence(kind="CLOUD_RESOURCE", locator="sg-123", verified=False)
    assert check([row(evidence=[cloud])], workspace)[0].status == "UNVERIFIED"
    known = check([row(evidence=[cloud])], workspace, cloud_resource_ids={"sg-123"})[0]
    assert known.status == "EVIDENCE_CHECKED"


def test_sandbox_verified_rows_are_never_lowered(workspace):
    verified = row(status="SANDBOX_VERIFIED", evidence=[code("main.go:99")])
    assert check([verified], workspace)[0].status == "SANDBOX_VERIFIED"
