"""The whole first cycle on the local k3d cluster, with a fake model (ticket 21).

Real: source checkout and analysis, the review gate, the Sandbox loop on `cmp-sandbox`, the
network rules, the test suite, the final publication. Fake: the model (scripted answers) and the
image build (a public podinfo digest stands in for the application image, D-12).
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.queues import ANALYSIS_QUEUE, SANDBOX_QUEUE, WORKFLOW_QUEUE
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.activities.k8s_workloads_cli import CommandRunner
from cmp_worker.activities.sandbox_loop import SandboxLoopActivities, make_proxy_suite_runner
from cmp_worker.sandbox_deploy import SANDBOX_CONTEXT, SandboxCluster, SandboxService
from cmp_worker.workers.build import create_worker as build_worker
from cmp_worker.workers.migration import create_activity_worker, create_workflow_worker
from cmp_worker.workers.provisioning import create_worker as provisioning_worker
from cmp_worker.workflows.migration import MigrationWorkflow
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "analysis"))
from pipeline_support import analysis_input, build_repo, make, row
from test_k3d import KUBECONFIG, PODINFO, ROOT

pytestmark = [
    pytest.mark.k3d,
    pytest.mark.skipif(not KUBECONFIG, reason="set CMP_SANDBOX_KUBECONFIG to run the k3d tests"),
]

ROWS = [
    row("postgres", "pgx driver", ["main.go:7"], grade="PARTIAL", required_proposed=False),
    row("environment-config", "db settings", ["main.go:11"]),
]


async def until(handle, query, predicate, tries=3000):
    for _ in range(tries):
        try:
            state = await handle.query(query)
        except RPCError:
            await asyncio.sleep(0.05)
            continue
        if predicate(state):
            return state
        await asyncio.sleep(0.05)
    pytest.fail(f"{query} did not reach the expected state")


@pytest.mark.asyncio
async def test_analysis_review_loop_and_publication_run_as_one_workflow(tmp_path):
    from cmp_worker.artifact_store import ArtifactRef

    repo = build_repo(tmp_path)
    run_id = "pl-" + uuid.uuid4().hex[:8]
    # The fixture app declares port 8080 and /health; podinfo listens on 9898 and serves /healthz.
    # The scripted Fixer proposes exactly that correction when the first attempt fails.
    fixer = FakeLLMClient(
        [
            LLMResponse(
                tool_calls=[
                    call(
                        "submit_requirements_fix",
                        reason="the application listens on 9898 and serves /healthz",
                        edits=[
                            {"resource_id": "orders-api", "field": "port", "value": 9898},
                            {
                                "resource_id": "orders-api",
                                "field": "health_path",
                                "value": "/healthz",
                            },
                        ],
                    )
                ]
            )
        ]
    )
    pipeline = make(
        repo,
        "k3d",
        llm_factory=lambda *_: FakeLLMClient(
            [LLMResponse(tool_calls=[call("submit_mapping", rows=list(ROWS))])]
        ),
    )
    service = SandboxService(
        SandboxCluster(
            CommandRunner(), kubeconfig=Path(KUBECONFIG).expanduser(), context=SANDBOX_CONTEXT
        ),
        database_chart_dir=ROOT / "infra/helm/postgres",
        application_chart_dir=ROOT / "infra/helm/application",
        helm_timeout="40s",
    )
    loop = SandboxLoopActivities(
        service=service,
        store=pipeline.store,
        workspace_root=tmp_path / "fixes",
        build=lambda resource_id, source: PODINFO,
        suite_runner=make_proxy_suite_runner(service),
        llm_factory=lambda *_: fixer,
    )
    data = {"project_id": "proj-1", "analysis": analysis_input(repo), "sandbox": {"run_id": run_id}}
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(
            env.client, task_queue=ANALYSIS_QUEUE, activities=AnalysisActivities(pipeline).all()
        ),
        Worker(env.client, task_queue=SANDBOX_QUEUE, activities=loop.all()),
        build_worker(env.client),
        provisioning_worker(env.client),
        create_activity_worker(env.client),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            MigrationWorkflow.run, data, id="migration-e2e", task_queue=WORKFLOW_QUEUE
        )
        child = env.client.get_workflow_handle("analysis-migration-e2e")
        state = await until(child, "review_state", lambda s: s["pending"] == "confirm_requirements")
        rows = {i["capability"]: i["id"] for i in state["items"]}
        ref = state["analysis_ref"]
        await child.signal(
            "confirm_requirements",
            {
                "analysis_ref": ref,
                "required": {rows["postgres"]: False, rows["environment-config"]: True},
                "actor": "alice",
            },
        )
        await until(child, "review_state", lambda s: s["pending"] == "approve_losses")
        await child.signal(
            "approve_losses",
            {"analysis_ref": ref, "approvals": {rows["postgres"]: "APPROVED"}, "actor": "bob"},
        )
        await until(handle, "status", lambda s: s["phase"] in {"awaiting_approval", "failed"})
        status = await handle.query("status")
        await handle.signal("approve")
        final = await handle.result()
        sandbox = await env.client.get_workflow_handle("sandbox-migration-e2e").result()

    assert final == "scaffold_complete" and status["failure_code"] is None, status
    assert sandbox["status"] == "PASSED"
    assert [(a["attempt"], a["passed"], a.get("failure_class")) for a in sandbox["attempts"]] == [
        (1, False, "SPEC"),
        (2, True, None),
    ]
    published = sandbox["published"]
    assert published["status"] == "PUBLISHED" and published["patch"] is None
    # What was published is exactly what the last verification used, and it is the fixed version.
    fixed = json.loads(
        pipeline.store.get(ArtifactRef.model_validate(published["requirements_ref"]))
    )
    api = next(r for r in fixed["resources"] if r["id"] == "orders-api")
    assert (api["port"], api["health_path"]) == (9898, "/healthz")
    assert published["requirements_ref"] != status["pipeline"]["requirements_ref"]
    from cmp_contracts.analysis import AnalysisReport

    final_report = AnalysisReport.model_validate_json(
        pipeline.store.get(ArtifactRef.model_validate(published["report_ref"]))
    )
    assert {r.capability: r.status for r in final_report.mapping}[
        "environment-config"
    ] == "SANDBOX_VERIFIED"
    # The Sandbox is gone.
    listing = await asyncio.to_thread(
        subprocess.run,
        ["kubectl", "--kubeconfig", str(Path(KUBECONFIG).expanduser()), "--context", SANDBOX_CONTEXT,
         "get", "ns", "-o", "name"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert run_id not in listing.stdout
