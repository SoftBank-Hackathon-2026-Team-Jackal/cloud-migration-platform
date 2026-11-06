"""부모 MigrationWorkflow의 data_copy 단계(ticket 05) 시험.

MigrationWorkflow를 상속한 시험용 Workflow로 `_data_migration`만 실행한다. 인계값 Activity와 데이터
이전 Activity는 가짜, 자식 DataMigrationWorkflow는 진짜다.
"""

from __future__ import annotations

import asyncio
import uuid

from cmp_contracts.queues import MIGRATION_QUEUE, PROVISIONING_QUEUE, WORKFLOW_QUEUE
from cmp_data.handoff import DataMigrationInput, target_endpoint
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from cmp_worker.workflows.migration import MigrationWorkflow
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from .test_data_workflow import Fakes

SOURCE = {"access": "AWS_EKS", "cluster_name": "cmp-demo-source", "region": "ap-northeast-2",
          "namespace": "chat-source", "pod": "chat-db-0", "peer_host": "10.200.0.10",
          "local_host": "chat-db.chat-source.svc", "dbname": "chat"}  # fmt: skip
GIVEN = {"source": SOURCE, "app": {}, "settings": {}, "target_peer_host": "10.200.0.20",
         "sample_interval_s": 1, "monitor_interval_s": 1}  # fmt: skip
READY_VIEW = {
    "proceed": True,
    "common": {"provider": "GCP", "namespace": "chat",
               "cluster": {"kind": "GKE", "name": "cmp-gke", "location": "asia-northeast3",
                           "account_id": "sbh26-prod"}},
    "database": {"release": "chat-db", "name": "chat", "ready": True},
}  # fmt: skip


# ---- 순수: 인계값 → 대상 DbEndpoint ----


def test_gke_handoff_becomes_a_gcp_endpoint_on_the_release_pod() -> None:
    target, code = target_endpoint(READY_VIEW, DataMigrationInput.model_validate(GIVEN))
    assert code is None
    assert (target.access, target.project_id, target.cluster_name) == (
        "GCP_GKE",
        "sbh26-prod",
        "cmp-gke",
    )
    assert (target.namespace, target.pod, target.peer_host) == ("chat", "chat-db-0", "10.200.0.20")


def test_blocked_handoff_and_missing_tunnel_address_are_codes() -> None:
    given = DataMigrationInput.model_validate(GIVEN)
    blocked = {"proceed": False, "blocked_by": ["DATABASE_NOT_READY"]}
    assert target_endpoint(blocked, given) == (None, "DATABASE_NOT_READY")
    no_peer = DataMigrationInput.model_validate(GIVEN | {"target_peer_host": None})
    assert target_endpoint(READY_VIEW, no_peer) == (None, "TARGET_PEER_HOST_MISSING")


def test_on_prem_uses_the_tunnel_endpoint_and_needs_a_kubeconfig() -> None:
    view = READY_VIEW | {
        "common": {"namespace": "chat", "cluster": {"kind": "K3s"}},
        "database": {"release": "chat-db", "name": "chat", "tunnel_endpoint": "10.200.0.2:5432"},
    }
    no_peer = GIVEN | {"target_peer_host": None}
    assert target_endpoint(view, DataMigrationInput.model_validate(no_peer)) == (
        None, "TARGET_DB_ACCESS_UNSUPPORTED")  # fmt: skip
    target, _ = target_endpoint(
        view, DataMigrationInput.model_validate(no_peer | {"target_kubeconfig_ref": "onprem"})
    )
    assert (target.access, target.peer_host, target.peer_port) == ("KUBECONFIG", "10.200.0.2", 5432)


# ---- 부모 단계 + 진짜 자식 ----


@workflow.defn(name="DataStepProbe")
class DataStepProbe(MigrationWorkflow):
    @workflow.run
    async def run(self, given: dict) -> str:
        self.plan = {"plan_id": "plan-1", "revision": 2}
        return (await self._data_migration(given)) or "continue"


def handoff_worker(client, view):
    @activity.defn(name="get_migration_handoff")
    async def handoff(request: dict) -> dict:
        assert request == {"plan_id": "plan-1", "revision": 2, "consumer": "DATA_MIGRATION"}
        return view

    return Worker(client, task_queue=PROVISIONING_QUEUE, activities=[handoff])


async def run_probe(view, drive, given=GIVEN):
    fakes = Fakes()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=WORKFLOW_QUEUE,
            workflows=[DataStepProbe, DataMigrationWorkflow],
        ),
        Worker(env.client, task_queue=MIGRATION_QUEUE, activities=fakes.activities()),
        handoff_worker(env.client, view),
    ):
        pid = f"migration-{uuid.uuid4()}"
        handle = await env.client.start_workflow(
            DataStepProbe.run, given, id=pid, task_queue=WORKFLOW_QUEUE
        )
        await drive(env, handle, pid)
        return await handle.result(), await handle.query("status")


async def test_parent_continues_only_after_the_db_is_switched() -> None:
    async def drive(env, handle, pid):
        child = env.client.get_workflow_handle(f"data-{pid}")
        for _ in range(200):
            state = (await handle.query("status"))["data_migration"]
            if state and state["phase"] == "streaming":
                break
            await asyncio.sleep(0.05)
        assert state["phase"] == "streaming"
        await child.signal(DataMigrationWorkflow.switchover)

    result, status = await run_probe(READY_VIEW, drive)
    assert result == "continue"
    assert status["data_migration"]["writer"] == "target"
    assert status["data_migration"]["phase"] == "switched"
    assert status["data_migration"]["active"] == "target"


async def test_parent_blocks_when_the_preflight_location_is_unknown() -> None:
    """Provisioning 76: 검사 위치(Agent·클러스터)를 모르면 자식(터널·복제)을 시작하지 않는다."""
    from .test_data_preflight_request import GIVEN as ONPREM_GIVEN

    view = READY_VIEW | {"common": {"provider": "ON_PREM", "namespace": "chat",
                                    "cluster": {"kind": "K3s", "agent_id": "office-seoul-01"}}}  # fmt: skip

    async def drive(env, handle, pid):
        pass

    result, status = await run_probe(view, drive, ONPREM_GIVEN)
    assert result == "data_blocked"
    assert status["failure_code"] == "NETWORK_PREFLIGHT_TARGET_MISSING"


async def test_parent_stops_when_the_handoff_is_not_ready() -> None:
    async def drive(env, handle, pid):
        pass

    result, status = await run_probe(
        {"proceed": False, "blocked_by": ["DATABASE_NOT_READY"]}, drive
    )
    assert result == "data_blocked"
    assert status["failure_code"] == "DATABASE_NOT_READY"


def test_start_input_carries_validated_data_migration_settings() -> None:
    import json

    import pytest
    from cmp_worker.start_input import build_start_input
    from pydantic import ValidationError

    data = build_start_input(project_id="p", repository="r", commit="c",
                             data_migration_json=json.dumps(GIVEN))  # fmt: skip
    assert data["data_migration"]["source"]["pod"] == "chat-db-0"
    assert "data_migration" not in build_start_input(project_id="p", repository="r", commit="c")
    with pytest.raises(ValidationError):
        build_start_input(project_id="p", repository="r", commit="c",
                          data_migration_json=json.dumps(GIVEN | {"password": "x"}))  # fmt: skip


@workflow.defn(name="GateProbe")
class GateProbe(MigrationWorkflow):
    @workflow.run
    async def run(self, given: dict) -> list:
        self.plan = {"plan_id": "plan-1", "revision": 2}
        gates = [self.db_gate_ok()]
        await self._data_migration(given)
        await workflow.wait_condition(lambda: self.db_gate_ok() is True)
        return [*gates, self.db_gate_ok()]


async def test_parent_db_gate_turns_true_once_reverse_replication_streams() -> None:
    """#128: Traffic은 부모의 db_gate_ok()를 읽는다. 전환 뒤 역방향 복제 감시가 streaming이면 True."""
    fakes = Fakes()
    async with await WorkflowEnvironment.start_time_skipping() as env:  # noqa: SIM117
        async with (
            Worker(
                env.client, task_queue=WORKFLOW_QUEUE, workflows=[GateProbe, DataMigrationWorkflow]
            ),
            Worker(env.client, task_queue=MIGRATION_QUEUE, activities=fakes.activities()),
            handoff_worker(env.client, READY_VIEW),
        ):
            pid = f"migration-{uuid.uuid4()}"
            handle = await env.client.start_workflow(
                GateProbe.run, GIVEN, id=pid, task_queue=WORKFLOW_QUEUE
            )
            child = env.client.get_workflow_handle(f"data-{pid}")
            for _ in range(200):
                state = (await handle.query("status"))["data_migration"]
                if state and state["phase"] == "streaming":
                    break
                await asyncio.sleep(0.05)
            await child.signal(DataMigrationWorkflow.switchover)
            assert await handle.result() == [None, True]
