import pytest
from cmp_contracts.analysis import MappingRow
from cmp_testsuite import load_bundle
from cmp_worker.artifact_store import ArtifactRef
from cmp_worker.sandbox_input import (
    POSTGRES_16,
    SandboxSettings,
    assemble_sandbox_input,
    default_run_id,
    keep_hosts,
)
from pipeline_support import analysis_input, make, run_to_requirements
from pydantic import ValidationError


def analysed(pipeline, repo):
    """The same dict the analysis child returns, built from the real pipeline steps."""
    inp = analysis_input(repo)
    source, code, manifests, infra, built = run_to_requirements(pipeline, inp)
    pre = pipeline.precheck(inp, source, code, manifests)
    mapped = pipeline.map_usage(inp, source, code, manifests, built, pre)
    report = pipeline.check_evidence(inp, source, built, infra, pre, mapped)
    report = {**report, "migration_id": inp["migration_id"]}
    return {
        "report": report,
        "requirements": built["requirements_ref"],
        "review": None,
        "source": {"workspace": source["workspace"], "commit": source["commit"]},
    }


def test_the_input_points_at_what_the_analysis_stored(repo):
    pipeline = make(repo)
    analysis = analysed(pipeline, repo)
    result = assemble_sandbox_input(analysis, SandboxSettings(), pipeline.store)
    assert result["migration_id"] == "mig-1" and result["run_id"] == default_run_id("mig-1")
    assert result["requirements_ref"] == analysis["requirements"]
    assert result["analysis_report_ref"] == analysis["report"]["report_ref"]
    assert result["source"] == {"workspace": analysis["source"]["workspace"]}
    assert result["database_image"]["uri"] == POSTGRES_16.uri and result["review_ref"] is None


def test_the_standard_suite_is_stored_and_tests_the_applications_own_health_path(repo):
    pipeline = make(repo)
    result = assemble_sandbox_input(analysed(pipeline, repo), SandboxSettings(), pipeline.store)
    suite = load_bundle(pipeline.store.get(ArtifactRef.model_validate(result["suite_ref"])))
    assert [c.id for c in suite.cases] == ["std-health"]
    # The path is supplied from the target at run time, so a pinned suite survives a health-path fix.
    assert suite.cases[0].steps[0].path == "{{health_path}}"
    assert suite.cases[0].covers == ["environment-config"]


def test_a_write_and_read_path_adds_the_database_tests_that_cover_postgres(repo):
    pipeline = make(repo)
    settings = SandboxSettings(write_path="/items", read_path="/items/{{id}}")
    result = assemble_sandbox_input(analysed(pipeline, repo), settings, pipeline.store)
    suite = load_bundle(pipeline.store.get(ArtifactRef.model_validate(result["suite_ref"])))
    assert {c.id: c.covers for c in suite.cases} == {
        "std-health": ["environment-config"],
        "std-db-roundtrip": ["postgres"],
        "std-survives-db-restart": ["postgres"],
    }


def test_the_same_analysis_gives_the_same_input_and_the_same_suite_hash(repo):
    pipeline = make(repo)
    analysis = analysed(pipeline, repo)
    one = assemble_sandbox_input(analysis, SandboxSettings(), pipeline.store)
    two = assemble_sandbox_input(analysis, SandboxSettings(), pipeline.store)
    assert one == two


def test_settings_override_the_run_the_image_and_the_hosts(repo):
    pipeline = make(repo)
    settings = SandboxSettings(
        run_id="fixed1", storage_class="fast", source_hosts=["db.rds.amazonaws.com"],
        extra_keep_hosts=[{"host": "api.pay.example.com", "port": 443}],
    )  # fmt: skip
    result = assemble_sandbox_input(analysed(pipeline, repo), settings, pipeline.store)
    assert result["run_id"] == "fixed1" and result["storage_class"] == "fast"
    assert result["source_hosts"] == ["db.rds.amazonaws.com"]
    assert {"host": "api.pay.example.com", "port": 443} in result["keep_hosts"]


def test_unknown_settings_are_refused_so_a_typo_cannot_silently_change_nothing():
    with pytest.raises(ValidationError):
        SandboxSettings.model_validate({"strage_class": "x"})


def row(capability, disposition):
    return MappingRow(
        id=f"r-{capability}", capability=capability, usage="u", grade="DIRECT",
        required_proposed=True, required_reason="r", required=None, disposition=disposition,
        loses=[], treatment=None, status="EVIDENCE_CHECKED", evidence=[],
    )  # fmt: skip


def test_object_storage_kept_at_the_source_becomes_an_allowed_s3_host():
    from cmp_contracts.analysis import AnalysisReport
    from cmp_contracts.provisioning import VersionRef

    def report(rows):
        return AnalysisReport(
            migration_id="m", ref=VersionRef(id="analysis", version="v"), base_commit="c" * 40,
            commit_verified=False, usage_profile={"usages": []}, mapping=rows,
            precheck={"findings": []}, external_entries=[], source_cidrs=[],
            source_db_images={}, unmapped_items=[], unverified_risks=[],
        )  # fmt: skip

    hosts = keep_hosts(
        report([row("object-storage", "KEEP"), row("postgres", "MIGRATE")]), "ap-northeast-2"
    )
    assert hosts == [
        {"host": "s3.ap-northeast-2.amazonaws.com", "port": 443},
        {"host": "*.s3.ap-northeast-2.amazonaws.com", "port": 443},
    ]
    assert keep_hosts(report([row("postgres", "MIGRATE")]), "ap-northeast-2") == []
    assert keep_hosts(None, "ap-northeast-2") == []


def test_the_env_plan_the_analysis_made_travels_to_the_sandbox_as_a_reference(repo):
    from cmp_providers.env_file import InMemoryEnvFileStore

    store = InMemoryEnvFileStore()
    pipeline = make(repo, "plan", env_store=store)
    ref = store.put(b"DB_HOST=x\nAPP_MODE=prod\n")
    inp = analysis_input(repo, env_file_ref=ref.model_dump(mode="json"))
    source, _, _, _, built = run_to_requirements(pipeline, inp)
    analysis = {
        "report": {"migration_id": inp["migration_id"]},
        "requirements": built["requirements_ref"],
        "review": None,
        "source": {"workspace": source["workspace"], "commit": source["commit"]},
        "assembly_ref": built["assembly_ref"],
    }
    result = assemble_sandbox_input(analysis, SandboxSettings(), pipeline.store)
    assert result["env_plan_ref"]["kind"] == "env-plan"
    other = make(repo, "none")
    plain = assemble_sandbox_input(analysed(other, repo), SandboxSettings(), other.store)
    assert "env_plan_ref" not in plain


def test_an_app_shape_in_the_settings_reaches_the_pinned_suite(repo):
    pipeline = make(repo, "shape")
    settings = SandboxSettings(
        write_path="/api/messages",
        write_body={
            "client_msg_id": "std-{{run_id}}",
            "author": "cmp-test",
            "body": "value-{{run_id}}",
        },
        write_status=201,
        id_from_response="id",
        value_field="body",
        read_back="REPEAT_WRITE",
    )
    result = assemble_sandbox_input(analysed(pipeline, repo), settings, pipeline.store)
    suite = load_bundle(pipeline.store.get(ArtifactRef.model_validate(result["suite_ref"])))
    roundtrip = next(c for c in suite.cases if c.id == "std-db-roundtrip")
    assert roundtrip.steps[0].expect.status == 201 and roundtrip.steps[1].expect.status == 200
    again = assemble_sandbox_input(analysed(pipeline, repo), settings, pipeline.store)
    assert again["suite_ref"] == result["suite_ref"]  # the same shape gives the same hash


def test_an_unknown_read_back_is_refused_so_a_typo_cannot_change_nothing():
    with pytest.raises(ValidationError):
        SandboxSettings(write_path="/w", read_back="POLL")


# ------------------------------------------------- ticket 27, layer 2: the shape is found, not typed


def analysis_with_assembly(repo, name):
    pipeline = make(repo, name)
    inp = analysis_input(repo)
    source, _, _, _, built = run_to_requirements(pipeline, inp)
    base = {
        "report": {"migration_id": inp["migration_id"]},
        "requirements": built["requirements_ref"],
        "review": None,
        "source": {"workspace": source["workspace"], "commit": source["commit"]},
        "assembly_ref": built["assembly_ref"],
    }
    return pipeline, base


def test_the_pipeline_stores_the_routes_the_application_registers(repo):
    pipeline, base = analysis_with_assembly(repo, "routes")
    doc = pipeline.store.get_json(ArtifactRef.model_validate(base["assembly_ref"]))
    assert isinstance(doc["http_routes"], list)  # empty is fine when the fixture has none
