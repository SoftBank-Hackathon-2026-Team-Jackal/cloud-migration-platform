from pathlib import Path

import pytest
from cmp_agents.catalog import CatalogError, load_catalog

ROOT = Path(__file__).parent.parent.parent
FIRST_CYCLE = {
    "container-runtime",
    "postgres",
    "environment-config",
    "network-policy",
    "ingress",
    "volume",
    "pod-identity",
    "object-storage",
}


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(ROOT / "catalog", skills_root=ROOT / "skills")


def test_first_version_covers_the_first_cycle_capabilities(catalog):
    assert catalog.capabilities == FIRST_CYCLE


def test_every_entry_grades_all_three_targets_and_lists_standard_tests(catalog):
    for entry in catalog.entries:
        assert {r.target for r in entry.grade_rules} == {"AWS", "GCP", "ON_PREM"}, entry.capability
        assert entry.usage_vocabulary and entry.detect_rules, entry.capability


def test_skills_referenced_by_the_catalog_exist(catalog):
    named = {e.skill for e in catalog.entries if e.skill}
    assert named <= {"k8s-workload", "db-postgres", "network", "config-env"}
    assert catalog.get("object-storage").skill is None


@pytest.mark.parametrize(
    ("evidence", "capability"),
    [
        ("alb.ingress.kubernetes.io/scheme: internet-facing", "ingress"),
        ("provisioner: ebs.csi.aws.com", "volume"),
        ("eks.amazonaws.com/role-arn", "pod-identity"),
        ("s3.PutObject at main.go:20", "object-storage"),
        ("CREATE EXTENSION pgcrypto", "postgres"),
        ("ENV_READ DATABASE_URL", "environment-config"),
    ],
)
def test_candidates_come_from_detect_rules(catalog, evidence, capability):
    assert capability in {e.capability for e in catalog.candidates(evidence)}


def test_unrelated_evidence_has_no_candidates(catalog):
    assert catalog.candidates("nothing to see here") == []


def write(tmp_path, name, body):
    (tmp_path / f"{name}.yaml").write_text(body)


VALID = """
capability: demo
title: Demo
usage_vocabulary: [a]
detect_rules:
  - {source: CODE, pattern: demo}
grade_rules:
  - {target: AWS, grade: DIRECT, when: always}
known_differences: []
standard_tests: []
skill: null
"""


def test_a_valid_entry_loads_and_matches_by_regex(tmp_path):
    write(tmp_path, "demo", VALID)
    loaded = load_catalog(tmp_path)
    assert loaded.capabilities == {"demo"}
    assert [e.capability for e in loaded.candidates("a DEMO call")] == ["demo"]


@pytest.mark.parametrize(
    ("name", "body", "reason"),
    [
        ("demo", VALID.replace("capability: demo", "capability: other"), "file name"),
        ("demo", VALID.replace("usage_vocabulary: [a]", "usage_vocabulary: []"), "schema"),
        ("demo", VALID.replace("grade: DIRECT", "grade: great"), "schema"),
        ("demo", VALID.replace("pattern: demo", "pattern: '('"), "regex"),
        ("demo", "not: [valid", "yaml"),
        ("demo", VALID.replace("skill: null", "skill: missing-skill"), "skill"),
    ],
)
def test_broken_entries_are_rejected(tmp_path, name, body, reason):
    write(tmp_path, name, body)
    with pytest.raises(CatalogError):
        load_catalog(tmp_path, skills_root=tmp_path / "skills")
