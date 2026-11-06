"""Ticket 29-A: the source's security measured against the target it is going to.

Seam: the pure `judge_security` (source measures x target state -> verdict) and
`target_security` (what the platform builds for a target, from its provider and the
requirements). Each case states its input and the verdict it must give. `LOWER` is what the
approval gate stops on; `UNKNOWN` is "ask a person", never an assumption of the same level.
"""

import pytest
from cmp_contracts.analysis import AnalysisInput, SecurityJudgement, SecurityMeasure
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_scanners import security_judge
from cmp_scanners.security_judge import (
    SUGGESTIONS,
    TargetSecurity,
    judge_security,
    target_security,
)
from cmp_worker.artifact_store import ArtifactRef
from pipeline_support import MAIN_GO, git, make

CRITERIA = ["EXTERNAL_HTTPS", "DB_ACCESS_SCOPE", "DB_TRANSPORT_ENCRYPTION", "ADMIN_API_EXPOSURE"]


def measure(criterion, state, *reasons):
    return SecurityMeasure(criterion=criterion, state=state, reasons=list(reasons))


def target(state, *reasons):
    return TargetSecurity(state=state, reasons=list(reasons))


def judged(criterion, source_state, target_state):
    result = judge_security([measure(criterion, source_state)], {criterion: target(target_state)})
    (only,) = result
    return only


def requirements(*, with_db=True):
    resources = [
        {
            "id": "api", "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 100,
            "memory_mib": 128, "port": 8080, "health_path": "/healthz", "env_bindings": {},
        }
    ]  # fmt: skip
    intents = []
    if with_db:
        resources.append(
            {
                "id": "db", "kind": "POSTGRESQL", "version": "16", "extensions": [],
                "database": {"name": "app", "username": "app"}, "cpu_millicores": 250,
                "memory_mib": 512, "storage_gib": 1,
            }
        )  # fmt: skip
        intents.append({"source_resource_id": "api", "destination_resource_id": "db", "port": 5432})
    return RequirementsV2.model_validate(
        {"contract_version": "2", "resources": resources, "network_intent": intents}
    )


# --- the comparison ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "tgt", "verdict"),
    [
        ("PROTECTED", "PROTECTED", "SAME"),
        ("PROTECTED", "PARTIAL", "LOWER"),
        ("PROTECTED", "UNPROTECTED", "LOWER"),
        ("PARTIAL", "UNPROTECTED", "LOWER"),
        ("PARTIAL", "PARTIAL", "SAME"),
        ("PARTIAL", "PROTECTED", "HIGHER"),
        ("UNPROTECTED", "PARTIAL", "HIGHER"),
        ("UNPROTECTED", "PROTECTED", "HIGHER"),
        ("UNPROTECTED", "UNPROTECTED", "SAME"),
        ("UNKNOWN", "PROTECTED", "UNKNOWN"),
        ("PROTECTED", "UNKNOWN", "UNKNOWN"),
        ("UNKNOWN", "UNKNOWN", "UNKNOWN"),
        ("NOT_APPLICABLE", "UNPROTECTED", "SAME"),
        ("NOT_APPLICABLE", "UNKNOWN", "SAME"),
        ("PROTECTED", "NOT_APPLICABLE", "SAME"),
    ],
)
def test_the_verdict_compares_the_source_with_the_target(source, tgt, verdict):
    assert judged("EXTERNAL_HTTPS", source, tgt).verdict == verdict


def test_unknown_is_never_read_as_the_same_level():
    assert judged("DB_ACCESS_SCOPE", "UNKNOWN", "UNPROTECTED").verdict == "UNKNOWN"


# --- weak sources and what would fix them -----------------------------------------------------


@pytest.mark.parametrize(
    ("source", "weak"),
    [("UNPROTECTED", True), ("PARTIAL", True), ("PROTECTED", False), ("UNKNOWN", False),
     ("NOT_APPLICABLE", False)],
)  # fmt: skip
def test_a_source_that_is_weak_to_begin_with_is_marked(source, weak):
    assert judged("DB_TRANSPORT_ENCRYPTION", source, "UNPROTECTED").weak_source is weak


def test_a_suggestion_comes_with_a_weak_source_or_a_loss_and_never_otherwise():
    weak_and_same = judged("DB_TRANSPORT_ENCRYPTION", "UNPROTECTED", "UNPROTECTED")
    lost = judged("EXTERNAL_HTTPS", "PROTECTED", "UNPROTECTED")
    fine = judged("EXTERNAL_HTTPS", "PROTECTED", "PROTECTED")
    assert weak_and_same.suggestion == SUGGESTIONS["DB_TRANSPORT_ENCRYPTION"] == "REQUIRE_DB_SSL"
    assert lost.suggestion == "ENFORCE_HTTPS_AT_ENTRY"
    assert fine.suggestion is None
    assert set(SUGGESTIONS) == set(CRITERIA)


def test_the_judgement_keeps_both_states_and_the_target_reasons():
    result = judge_security(
        [measure("EXTERNAL_HTTPS", "PROTECTED")],
        {"EXTERNAL_HTTPS": target("UNPROTECTED", "TARGET_NO_TLS")},
    )
    assert result == [
        SecurityJudgement(
            criterion="EXTERNAL_HTTPS",
            source_state="PROTECTED",
            target_state="UNPROTECTED",
            verdict="LOWER",
            weak_source=False,
            reasons=["TARGET_NO_TLS"],
            suggestion="ENFORCE_HTTPS_AT_ENTRY",
        )
    ]


def test_a_criterion_the_target_says_nothing_about_is_unknown():
    (only,) = judge_security([measure("EXTERNAL_HTTPS", "PROTECTED")], {})
    assert only.target_state == "UNKNOWN" and only.verdict == "UNKNOWN"


def test_the_same_input_gives_the_same_judgements_in_the_same_order():
    measures = [measure(c, "PROTECTED") for c in reversed(CRITERIA)]
    targets = {c: target("PROTECTED") for c in CRITERIA}
    first = judge_security(measures, targets)
    assert first == judge_security(measures, targets)
    assert [j.criterion for j in first] == CRITERIA  # fixed order, whatever the input order


# --- what the platform builds for a target ----------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "state", "reason"),
    [
        ("TRUSTED", "PROTECTED", "TARGET_ENTRY_TLS_AT_RELAY"),
        ("UNTRUSTED", "PARTIAL", "TARGET_TLS_UNTRUSTED_CERT"),
        ("NONE", "UNPROTECTED", "TARGET_ENTRY_WITHOUT_TLS"),
    ],
)
def test_the_on_prem_entry_follows_the_tls_the_relay_offers(monkeypatch, entry, state, reason):
    monkeypatch.setattr(security_judge, "ON_PREM_ENTRY_TLS", entry)
    result = target_security("ON_PREM", requirements(), [])["EXTERNAL_HTTPS"]
    assert result.state == state and reason in result.reasons


def test_the_relay_serves_a_trusted_certificate_so_the_on_prem_entry_is_protected():
    result = target_security("ON_PREM", requirements(), [])["EXTERNAL_HTTPS"]
    assert result.state == "PROTECTED"


@pytest.mark.parametrize("provider", ["AWS", "GCP"])
def test_a_cloud_entry_is_not_known_yet(provider):
    state = target_security(provider, requirements(), [])["EXTERNAL_HTTPS"]
    assert state.state == "UNKNOWN" and state.reasons == ["TARGET_ENTRY_NOT_DEFINED"]


@pytest.mark.parametrize("provider", ["AWS", "GCP", "ON_PREM"])
def test_the_database_is_behind_a_default_deny_policy_whatever_the_target(provider):
    state = target_security(provider, requirements(), [])["DB_ACCESS_SCOPE"]
    assert state.state == "PROTECTED" and "TARGET_DEFAULT_DENY_NETWORK_POLICY" in state.reasons


def test_without_requirements_or_a_database_the_target_database_access_is_unknown():
    assert target_security("ON_PREM", None, [])["DB_ACCESS_SCOPE"].state == "UNKNOWN"
    no_db = target_security("ON_PREM", requirements(with_db=False), [])["DB_ACCESS_SCOPE"]
    assert no_db.state == "UNKNOWN"


@pytest.mark.parametrize("provider", ["AWS", "GCP", "ON_PREM"])
def test_the_platforms_database_does_not_offer_ssl(provider):
    state = target_security(provider, requirements(), [])["DB_TRANSPORT_ENCRYPTION"]
    assert state.state == "UNPROTECTED" and state.reasons == ["TARGET_DB_SSL_NOT_CONFIGURED"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("UNPROTECTED", "UNPROTECTED"),
        ("PARTIAL", "UNPROTECTED"),
        ("PROTECTED", "UNPROTECTED"),  # the target entry does not filter paths
        ("UNKNOWN", "UNKNOWN"),
        ("NOT_APPLICABLE", "NOT_APPLICABLE"),
    ],
)
def test_the_target_entry_does_not_filter_admin_paths(source, expected):
    source_measures = [measure("ADMIN_API_EXPOSURE", source)]
    state = target_security("ON_PREM", requirements(), source_measures)["ADMIN_API_EXPOSURE"]
    assert state.state == expected


def test_an_unknown_provider_makes_every_target_state_unknown():
    states = target_security(None, requirements(), [])
    assert {c: s.state for c, s in states.items()} == {c: "UNKNOWN" for c in CRITERIA}
    assert all(s.reasons == ["TARGET_PROVIDER_UNKNOWN"] for s in states.values())


# --- the demo's picture, end to end on measures ----------------------------------------------


def test_the_demo_source_against_an_on_prem_target(monkeypatch):
    monkeypatch.setattr(
        security_judge, "ON_PREM_ENTRY_TLS", "NONE"
    )  # the entry before the relay had TLS
    source = [
        measure("EXTERNAL_HTTPS", "PROTECTED"),
        measure("DB_ACCESS_SCOPE", "UNPROTECTED"),
        measure("DB_TRANSPORT_ENCRYPTION", "UNPROTECTED"),
        measure("ADMIN_API_EXPOSURE", "UNPROTECTED"),
    ]
    result = judge_security(source, target_security("ON_PREM", requirements(), source))
    by = {j.criterion: j for j in result}
    assert (
        by["EXTERNAL_HTTPS"].verdict == "LOWER"
    )  # CloudFront HTTPS has no counterpart without TLS
    assert by["DB_ACCESS_SCOPE"].verdict == "HIGHER"  # default deny where every pod could connect
    assert (by["DB_TRANSPORT_ENCRYPTION"].verdict, by["DB_TRANSPORT_ENCRYPTION"].weak_source) == (
        "SAME",
        True,
    )
    assert (by["ADMIN_API_EXPOSURE"].verdict, by["ADMIN_API_EXPOSURE"].weak_source) == (
        "SAME",
        True,
    )


def test_the_demo_source_against_an_on_prem_target_with_tls_at_the_relay():
    """With the relay serving a trusted certificate nothing is lowered for the demo source: the entry
    keeps its HTTPS and the two weak criteria stay as weak as they were."""
    source = [
        measure("EXTERNAL_HTTPS", "PROTECTED"),
        measure("DB_ACCESS_SCOPE", "UNPROTECTED"),
        measure("DB_TRANSPORT_ENCRYPTION", "UNPROTECTED"),
        measure("ADMIN_API_EXPOSURE", "UNPROTECTED"),
    ]
    by = {
        j.criterion: j
        for j in judge_security(source, target_security("ON_PREM", requirements(), source))
    }
    assert by["EXTERNAL_HTTPS"].verdict == "SAME"
    assert not any(j.verdict == "LOWER" for j in by.values())


# --- the report -----------------------------------------------------------------------------


def judgements_of(repo, main_go, **input_fields):
    work = repo["work"]
    (work / "main.go").write_text(main_go)
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


def test_the_report_carries_the_judgements_and_flags_a_weak_source(repo):
    source = MAIN_GO.replace(
        '_ = os.Getenv("DB_HOST")', 'dsn := "host=db sslmode=disable"\n\t_ = dsn'
    )
    full = judgements_of(repo, source, target_provider="ON_PREM")
    by = {j["criterion"]: j for j in full["security_judgements"]}
    assert list(by) == CRITERIA
    encryption = by["DB_TRANSPORT_ENCRYPTION"]
    assert encryption["verdict"] == "SAME" and encryption["weak_source"] is True
    assert encryption["suggestion"] == "REQUIRE_DB_SSL"
    assert "SECURITY_WEAK_SOURCE: DB_TRANSPORT_ENCRYPTION" in full["unverified_risks"]


def test_a_report_without_a_target_has_only_unknown_verdicts(repo):
    full = judgements_of(repo, MAIN_GO)
    assert {j["verdict"] for j in full["security_judgements"]} == {"UNKNOWN"}
