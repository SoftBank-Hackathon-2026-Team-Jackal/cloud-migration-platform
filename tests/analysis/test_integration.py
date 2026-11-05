import asyncio
import contextlib
import os

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.queues import ANALYSIS_QUEUE, WORKFLOW_QUEUE
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.workers.migration import create_workflow_worker
from pipeline_support import MAIN_GO, SECRET, analysis_input, git, make, row
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

REF_ID = "analysis"


def mapping_llm(*rows):
    return lambda: FakeLLMClient(
        [LLMResponse(tool_calls=[call("submit_mapping", rows=list(rows))])]
    )


ROWS = [
    row("postgres", "pgx driver", ["main.go:7"], grade="PARTIAL", required_proposed=False),
    row("environment-config", "db settings", ["main.go:11"]),
]


@contextlib.asynccontextmanager
async def running(pipeline):
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        activities = AnalysisActivities(pipeline)
        async with (
            Worker(env.client, task_queue=ANALYSIS_QUEUE, activities=activities.all()),
            create_workflow_worker(env.client),
        ):
            yield env


async def start(env, inp, workflow_id="analysis-mig-1"):
    return await env.client.start_workflow(
        "AnalysisWorkflow", inp, id=workflow_id, task_queue=WORKFLOW_QUEUE
    )


async def until(handle, predicate, tries=300):
    for _ in range(tries):
        state = await handle.query("review_state")
        if predicate(state):
            return state
        await asyncio.sleep(0.02)
    pytest.fail("state not reached")


@pytest.mark.asyncio
async def test_the_analysis_runs_to_the_review_gate_with_real_steps(repo):
    pipeline = make(repo, llm_factory=mapping_llm(*ROWS))
    async with running(pipeline) as env:
        handle = await start(env, analysis_input(repo))
        state = await until(handle, lambda s: s["pending"] == "confirm_requirements")
        assert {i["capability"] for i in state["items"]} == {"postgres", "environment-config"}
        ref = state["analysis_ref"]
        rows = {i["capability"]: i["id"] for i in state["items"]}
        await handle.signal(
            "confirm_requirements",
            {
                "analysis_ref": ref,
                "required": {rows["postgres"]: False, rows["environment-config"]: True},
                "actor": "alice",
            },
        )
        await until(handle, lambda s: s["pending"] == "approve_losses")
        await handle.signal(
            "approve_losses",
            {"analysis_ref": ref, "approvals": {rows["postgres"]: "APPROVED"}, "actor": "bob"},
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result["review"]["approvals"] == {rows["postgres"]: "APPROVED"}
    assert result["requirements"]["kind"] == "requirements"
    assert result["report"]["commit"] == repo["sha"]


@pytest.mark.asyncio
async def test_workflow_history_holds_references_not_source_or_secrets(repo):
    pipeline = make(repo, llm_factory=mapping_llm(*ROWS))
    async with running(pipeline) as env:
        handle = await start(env, analysis_input(repo))
        await until(handle, lambda s: s["pending"] == "confirm_requirements")
        history = (await handle.fetch_history()).to_json()
    assert (
        SECRET not in history and "package main" not in history and "pgx.ErrNoRows" not in history
    )
    assert len(history) < 60_000


@pytest.mark.asyncio
async def test_the_same_input_gives_the_same_requirements_ref_across_runs(repo):
    refs = []
    for name in ("one", "two"):
        pipeline = make(repo, name)  # no LLM: mapping is skipped and the run finishes
        async with running(pipeline) as env:
            result = await asyncio.wait_for(
                (await start(env, analysis_input(repo))).result(), timeout=20
            )
        refs.append(result["requirements"]["sha256"])
    assert refs[0] == refs[1]


@pytest.mark.asyncio
async def test_without_an_llm_or_aws_the_run_finishes_and_says_what_was_not_collected(repo):
    pipeline = make(repo)
    async with running(pipeline) as env:
        result = await asyncio.wait_for(
            (await start(env, analysis_input(repo))).result(), timeout=20
        )
    assert result["review"] is None
    from cmp_worker.artifact_store import ArtifactRef

    report = pipeline.store.get_json(ArtifactRef.model_validate(result["report"]["report_ref"]))
    risks = " ".join(report["unverified_risks"])
    assert "INFRA_NOT_COLLECTED" in risks and "MAPPING_NOT_RUN" in risks


@pytest.mark.asyncio
async def test_an_extension_blocks_the_analysis_before_mapping(repo):
    work = repo["work"]
    (work / "db").mkdir()
    (work / "db/001.sql").write_text("CREATE EXTENSION pgcrypto;\n")
    git(work, "add", "-f", ".")
    git(work, "commit", "-q", "-m", "ext")
    git(work, "push", "-q", repo["url"], "HEAD:refs/heads/main")
    sha = git(work, "rev-parse", "HEAD")
    inp = {**analysis_input(repo), "requested_commit": sha}
    async with running(make(repo, llm_factory=mapping_llm(*ROWS))) as env:
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for((await start(env, inp)).result(), timeout=20)
    assert failure.value.cause.type == "ANALYSIS_BLOCKED"


@pytest.mark.asyncio
async def test_a_bad_commit_fails_fast_without_retries(repo):
    inp = {**analysis_input(repo), "requested_commit": "f" * 40}
    async with running(make(repo)) as env:
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for((await start(env, inp)).result(), timeout=20)
    assert failure.value.cause.cause.type == "SOURCE_FETCH_FAILED"


@pytest.mark.asyncio
async def test_a_string_input_still_runs_the_scaffold_path():
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
    ) as env:
        from cmp_worker.workers.analysis import create_worker as analysis_worker

        async with analysis_worker(env.client), create_workflow_worker(env.client):
            result = await asyncio.wait_for(
                env.client.execute_workflow(
                    "AnalysisWorkflow", "mig-9", id="analysis-mig-9", task_queue=WORKFLOW_QUEUE
                ),
                timeout=20,
            )
    assert result["review"] is None and result["report"]["ref"]["id"] == REF_ID


def test_the_fixture_source_has_the_lines_the_mapping_rows_point_at():
    lines = MAIN_GO.splitlines()
    assert "pgx" in lines[6] and "DB_HOST" in lines[10]


def test_the_worker_uses_real_analysis_steps_only_when_storage_and_workspace_are_configured(
    tmp_path,
):
    from cmp_worker.config import Settings
    from cmp_worker.workers.analysis import build_pipeline

    assert build_pipeline(Settings(_env_file=None)) is None
    configured = Settings(
        _env_file=None,
        analysis_artifact_store_dir=str(tmp_path / "store"),
        analysis_workspace_root=str(tmp_path / "ws"),
    )
    pipeline = build_pipeline(configured)
    assert pipeline is not None and pipeline.llm_factory is None
    assert (pipeline.skills_root / "k8s-workload" / "SKILL.md").is_file()
    assert (pipeline.catalog_dir / "postgres.yaml").is_file()
    assert pipeline.allow_local is False
