import hashlib
from pathlib import Path

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.analysis import AnalysisInput
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_scanners.aws_infra import AwsScanError
from cmp_worker.artifact_store import ArtifactRef
from pipeline_support import (
    AWS_KEY,
    MAIN_GO,
    SECRET,
    analysis_input,
    git,
    make,
    row,
    run_to_requirements,
)


def stored(pipeline, ref):
    return pipeline.store.get(ArtifactRef.model_validate(ref))


def test_fetch_source_pins_the_commit_and_returns_names_only(repo):
    source = make(repo).fetch_source(analysis_input(repo))
    assert source["commit"] == repo["sha"] and source["commit_verified"] is False
    assert source["env_names"] == {".env": ["APP_PORT", "DB_PASSWORD"]}
    assert Path(source["workspace"]).name == repo["sha"]
    assert SECRET not in str(source)


def test_a_scaffold_input_without_a_repository_is_refused(repo):
    with pytest.raises(ValueError):
        make(repo).fetch_source({"migration_id": "mig-1"})


def test_scans_are_stored_by_reference(repo):
    pipeline = make(repo)
    inp = analysis_input(repo)
    source = pipeline.fetch_source(inp)
    code = pipeline.scan_code(inp, source)
    manifests = pipeline.scan_manifests(inp, source)
    assert {
        s["target"]
        for s in pipeline.store.get_json(ArtifactRef.model_validate(code))["call_sites"]
        if s["kind"] == "ENV_READ"
    } >= {"DB_HOST", "FEATURE_X"}
    assert {
        w["name"]
        for w in pipeline.store.get_json(ArtifactRef.model_validate(manifests))["workloads"]
    } == {"orders-api", "orders-db"}
    assert set(code) == {"id", "version", "kind", "sha256", "size_bytes"}


def test_the_aws_scan_is_marked_not_collected_without_configuration(repo):
    infra = make(repo).scan_aws_infra(analysis_input(repo))
    assert infra["status"] == "NOT_COLLECTED"


def test_an_aws_failure_is_reported_with_the_missing_permission_and_the_run_continues(repo):
    def denied(inp):
        raise AwsScanError(
            "PERMISSION_DENIED", permission="ec2:DescribeVpcs", operation="DescribeVpcs"
        )

    inp = analysis_input(repo, cluster_name="shop", credential_ref="demo-source")
    infra = make(repo, aws_clients_factory=denied).scan_aws_infra(inp)
    assert infra == {
        "status": "FAILED",
        "code": "PERMISSION_DENIED",
        "permission": "ec2:DescribeVpcs",
    }


def test_requirements_combine_manifests_code_and_network_intents(repo):
    pipeline = make(repo)
    *_, built = run_to_requirements(pipeline, analysis_input(repo))
    data = stored(pipeline, built["requirements_ref"])
    assert hashlib.sha256(data).hexdigest() == built["requirements_ref"]["sha256"]
    requirements = RequirementsV2.model_validate_json(data)
    assert {r.id for r in requirements.resources} == {"orders-api", "orders-db"}
    intents = {
        (n.source_resource_id, n.destination_resource_id, n.external_host, n.port)
        for n in requirements.network_intent
    }
    assert intents == {
        ("orders-api", "orders-db", None, 5432),
        ("orders-api", None, "api.pay.example.com", 443),
    }
    assembly = pipeline.store.get_json(ArtifactRef.model_validate(built["assembly_ref"]))
    assert assembly["dockerfile"]["primary"] == "Dockerfile"
    assert "DB_HOST" in assembly["env_exceptions"]["replaced"]


def test_the_same_input_gives_the_same_requirements_ref(repo):
    first = run_to_requirements(make(repo, "one"), analysis_input(repo))[-1]
    second = run_to_requirements(make(repo, "two"), analysis_input(repo))[-1]
    assert first["requirements_ref"]["sha256"] == second["requirements_ref"]["sha256"]


def test_an_extension_blocks_the_requirements_with_the_reason(repo):
    work = repo["work"]
    (work / "db").mkdir()
    (work / "db/001.sql").write_text("CREATE EXTENSION pgcrypto;\n")
    git(work, "add", "-f", ".")
    git(work, "commit", "-q", "-m", "ext")
    git(repo["tmp"], "clone", "-q", "--bare", str(work), str(repo["tmp"] / "ext.git"))
    sha = git(work, "rev-parse", "HEAD")
    inp = AnalysisInput(
        migration_id="mig-2",
        repository=str(repo["tmp"] / "ext.git"),
        requested_commit=sha,
        manifests_dir="k8s",
    ).model_dump(mode="json")
    *_, built = run_to_requirements(make(repo, "ext"), inp)
    assert built["requirements_ref"] is None
    assert [b["feature"] for b in built["blocks"]] == ["PostgreSQL 확장 pgcrypto"]


def test_precheck_is_stored(repo):
    pipeline = make(repo)
    inp = analysis_input(repo)
    source = pipeline.fetch_source(inp)
    ref = pipeline.precheck(
        inp, source, pipeline.scan_code(inp, source), pipeline.scan_manifests(inp, source)
    )
    assert "findings" in pipeline.store.get_json(ArtifactRef.model_validate(ref["ref"]))


def test_mapping_is_skipped_when_no_llm_is_configured(repo):
    pipeline = make(repo)
    inp = analysis_input(repo)
    source, code, manifests, _infra, built = run_to_requirements(pipeline, inp)
    result = pipeline.map_usage(inp, source, code, manifests, built)
    assert result == {"status": "NOT_RUN", "rows_ref": None, "reason": "LLM_NOT_CONFIGURED"}


def test_the_mapper_sees_only_masked_text_and_the_report_checks_its_evidence(repo):
    (repo["work"] / "main.go").write_text(MAIN_GO + f'\nvar secret = "{AWS_KEY}"\n')
    git(repo["work"], "commit", "-q", "-am", "secret")
    git(repo["work"], "push", "-q", str(repo["url"]), "HEAD:refs/heads/main")
    sha = git(repo["work"], "rev-parse", "HEAD")
    llm = FakeLLMClient(
        [
            LLMResponse(
                tool_calls=[
                    call(
                        "submit_mapping",
                        rows=[
                            row("postgres", "pgx driver", ["main.go:7"]),
                            row("environment-config", "db settings", ["main.go:11"]),
                            row("ingress", "made up", ["main.go:99"]),
                        ],
                    )
                ]
            )
        ]
    )
    pipeline = make(repo, llm_factory=lambda *_: llm)
    inp = AnalysisInput(
        migration_id="mig-1", repository=repo["url"], requested_commit=sha, manifests_dir="k8s"
    ).model_dump(mode="json")
    source, code, manifests, infra, built = run_to_requirements(pipeline, inp)
    pre = pipeline.precheck(inp, source, code, manifests)
    mapped = pipeline.map_usage(inp, source, code, manifests, built, pre)
    assert mapped["status"] == "MAPPED"
    assert AWS_KEY not in " ".join(m.content for m in llm.calls[0].messages) + llm.calls[0].system
    report = pipeline.check_evidence(inp, source, built, infra, pre, mapped)
    statuses = {r["capability"]: r["status"] for r in report["mapping"]}
    assert statuses == {
        "postgres": "EVIDENCE_CHECKED",
        "environment-config": "EVIDENCE_CHECKED",
        "ingress": "UNVERIFIED",
    }
    full = pipeline.store.get_json(ArtifactRef.model_validate(report["report_ref"]))
    assert full["base_commit"] == sha and full["commit_verified"] is False
    assert full["source_db_images"] == {"orders-db": "postgres:16"}
    assert any("INFRA_NOT_COLLECTED" in r for r in full["unverified_risks"])


def test_no_secret_reaches_any_stored_artifact_or_returned_value(repo):
    pipeline = make(repo)
    inp = analysis_input(repo)
    source, code, manifests, infra, built = run_to_requirements(pipeline, inp)
    pre = pipeline.precheck(inp, source, code, manifests)
    mapped = pipeline.map_usage(inp, source, code, manifests, built, pre)
    report = pipeline.check_evidence(inp, source, built, infra, pre, mapped)
    blobs = [p.read_bytes().decode(errors="ignore") for p in (pipeline.store.root).iterdir()]
    assert not any(SECRET in b or "p4ss" in b for b in blobs)
    assert SECRET not in str([source, built, pre, mapped, report])
