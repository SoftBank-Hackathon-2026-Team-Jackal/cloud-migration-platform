"""ENV-05: analysis reads key names from `env_file_ref` and writes an EnvPlan (names only)."""

import pytest
from cmp_contracts.env_plan import EnvPlan, to_env_exceptions
from cmp_providers.env_file import EnvFileError, InMemoryEnvFileStore
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.artifact_store import ArtifactRef
from pipeline_support import analysis_input, make
from temporalio.exceptions import ApplicationError

ENV = (
    "DB_HOST=old-db.internal\nDB_PASSWORD=old-password-value\nAPP_MODE=prod\n"
    "AWS_REGION=ap-northeast-2\nLEGACY_ONLY=1\nDB_DSN=host=old dbname=x\n"
)


def with_ref(repo, store, text=ENV):
    ref = store.put(text.encode())
    return analysis_input(repo, env_file_ref=ref.model_dump(mode="json")), ref


def run(pipeline, inp):
    source = pipeline.fetch_source(inp)
    code = pipeline.scan_code(inp, source)
    manifests = pipeline.scan_manifests(inp, source)
    infra = pipeline.scan_aws_infra(inp)
    return source, pipeline.build_requirements(inp, source, code, manifests, infra)


def plan_of(pipeline, built):
    assembly = pipeline.store.get_json(ArtifactRef.model_validate(built["assembly_ref"]))
    ref = assembly["env_plan_ref"]
    return assembly, EnvPlan.model_validate(
        pipeline.store.get_json(ArtifactRef.model_validate(ref))
    )


def test_key_names_come_from_the_reference_not_from_the_repository_files(repo):
    store = InMemoryEnvFileStore()
    pipeline = make(repo, "e1", env_store=store)
    inp, _ = with_ref(repo, store)
    source = pipeline.fetch_source(inp)
    names = {n for found in source["env_names"].values() for n in found}
    assert "LEGACY_ONLY" in names and "DB_HOST" in names
    assert store.read_values_calls == 0  # analysis never reads a value


def test_the_plan_classifies_every_key_and_matches_the_web_facing_exceptions(repo):
    store = InMemoryEnvFileStore()
    pipeline = make(repo, "e2", env_store=store)
    inp, ref = with_ref(repo, store)
    _, built = run(pipeline, inp)
    assembly, plan = plan_of(pipeline, built)
    kinds = {e.key: e.disposition for e in plan.entries}
    assert kinds["AWS_REGION"] == "EXCLUDED" and kinds["APP_MODE"] == "PASS"
    assert kinds["DB_HOST"] == "REPLACED" and kinds["DB_PASSWORD"] == "REPLACED"
    assert kinds["DB_DSN"] == "UNREPRESENTABLE"  # key=value DSNs cannot be bound
    assert plan.env_file_ref == ref
    db_id = next(iter(assembly["env_exceptions"]["replaced"].values())).split(".")[0]
    assert (
        to_env_exceptions(plan, db_resource_id=db_id).model_dump(mode="json")
        == (assembly["env_exceptions"])
    )
    assert store.read_values_calls == 0


def test_without_a_reference_nothing_changes_and_no_plan_is_made(repo):
    pipeline = make(repo, "e3", env_store=InMemoryEnvFileStore())
    _, built = run(pipeline, analysis_input(repo))
    assembly = pipeline.store.get_json(ArtifactRef.model_validate(built["assembly_ref"]))
    assert "env_plan_ref" not in assembly


def test_a_reference_without_a_store_fails_with_a_code(repo):
    store = InMemoryEnvFileStore()
    inp, _ = with_ref(repo, store)
    with pytest.raises(EnvFileError) as exc:
        make(repo, "e4").fetch_source(inp)
    assert exc.value.code == "ENV_FILE_STORE_UNCONFIGURED"


@pytest.mark.asyncio
async def test_an_expired_reference_is_a_failure_that_is_not_retried(repo):
    store = InMemoryEnvFileStore()
    inp, ref = with_ref(repo, store)
    store.expire(ref)
    with pytest.raises(ApplicationError) as exc:
        await AnalysisActivities(make(repo, "e5", env_store=store)).fetch_source(inp)
    assert exc.value.type == "ENV_FILE_EXPIRED" and exc.value.non_retryable
    assert "old-password-value" not in str(exc.value)
