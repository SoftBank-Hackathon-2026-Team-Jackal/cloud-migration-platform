"""Issue #226: a real-pipeline MigrationWorkflow start → analysis child → events on the parent ID.

The CLI (`build_start_input`) and web (`start_request.start_input`, equal per
test_web_start_request) inputs put `project_id` into `AnalysisInput.migration_id`; the parent
replaces it with its own Workflow ID (migration.py, analysis child start), so the web screen's SSE
stream `GET /migrations/{id}/events` sees the analysis events.
"""

import asyncio
import contextlib
import json
import os
from uuid import uuid4

import pytest
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.migration_events import DbEventEmitter, list_events
from cmp_api.routes.migrations import get_event_sessions, get_status_reader
from cmp_contracts.queues import ANALYSIS_QUEUE, WORKFLOW_QUEUE
from cmp_worker.activities.analysis import AnalysisActivities
from cmp_worker.events import configure_emitter
from cmp_worker.workers.migration import create_workflow_worker
from fastapi.testclient import TestClient
from pipeline_support import analysis_input, make
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

PROJECT = "proj-demo"


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'events.db'}", connect_args={"timeout": 30}
    )
    Base.metadata.create_all(engine)
    previous = configure_emitter(DbEventEmitter(sessionmaker(bind=engine)))
    yield sessionmaker(bind=engine)
    configure_emitter(previous)
    app.dependency_overrides.clear()
    engine.dispose()


@contextlib.asynccontextmanager
async def running(pipeline):
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        Worker(
            env.client,
            task_queue=ANALYSIS_QUEUE,
            activities=AnalysisActivities(pipeline).all(),
        ),
        create_workflow_worker(env.client),
    ):
        yield env


async def until_done(sessions, migration_id, tries=600):
    for _ in range(tries):
        with sessions() as session:
            events = list_events(session, migration_id, after=0, limit=500)
        if any(e.type == "analysis.done" for e in events):
            return events
        await asyncio.sleep(0.05)
    pytest.fail("analysis.done not stored")


@pytest.mark.asyncio
async def test_analysis_events_land_on_the_parent_id_and_stream_over_sse(repo, sessions):
    # The start input as `build_start_input` / the web start shape it: migration_id = project_id.
    start = {
        "project_id": PROJECT,
        "analysis": {**analysis_input(repo), "migration_id": PROJECT},
        "sandbox": None,
    }
    migration_id = f"migration-{uuid4()}"  # routes/migrations.py start
    async with running(make(repo)) as env:
        handle = await env.client.start_workflow(
            "MigrationWorkflow", start, id=migration_id, task_queue=WORKFLOW_QUEUE
        )
        events = await until_done(sessions, migration_id)
        await handle.terminate("test: analysis checked")  # the Sandbox step has no worker here
    with sessions() as session:
        assert list_events(session, PROJECT, after=0, limit=500) == []
    assert [e.data["activity"] for e in events if e.type == "analysis.scan"] and {
        e.data["activity"] for e in events if e.type == "analysis.scan"
    } == {"fetch_source", "scan_code", "scan_manifests", "scan_aws_infra", "scan_network"}

    async def read(_):
        return {"phase": "sandbox", "approval_pending": False, "cancel_requested": False}

    app.dependency_overrides[get_event_sessions] = lambda: sessions
    app.dependency_overrides[get_status_reader] = lambda: read
    with TestClient(app).stream(
        "GET", f"/migrations/{migration_id}/events?legacy=false"
    ) as response:
        first = next(line for line in response.iter_lines() if line.startswith("data: "))
    snapshot = json.loads(first.removeprefix("data: "))
    assert snapshot["type"] == "snapshot" and snapshot["data"]["migration_id"] == migration_id
    types = [e["type"] for e in snapshot["data"]["events"]]
    assert types.count("analysis.scan") == 5 and types[-1] == "analysis.done"
