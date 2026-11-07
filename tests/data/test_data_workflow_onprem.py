"""Provisioning 76: 온프레미스(ADR-007 2안) 데이터 Workflow 연결.

터널 → 네트워크 사전 점검(MNP, 같은 입력 Fingerprint의 PASS만 통과) → 복제 순서와, 터널을 만든 실행이
끝날 때(완료·실패·취소·차단) `dm_tunnel_cleanup`을 부르는지 Temporal 위에서 본다. Activity는 가짜다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from cmp_contracts.network_preflight import NetworkPreflightRequest
from cmp_contracts.queues import MIGRATION_QUEUE, WORKFLOW_QUEUE
from cmp_providers.network_preflight import input_fingerprint
from cmp_worker.workflows import data_migration as dmw
from cmp_worker.workflows.data_migration import DataMigrationWorkflow
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from .test_data_workflow import ARGS, Fakes, wait_announced, wait_phase

TUNNEL = {"ingress": "source", "target_egress_cidrs": ["203.0.113.7/32"]}
PREFLIGHT = NetworkPreflightRequest(
    migration_id="migration-x",
    source_db_host="cmp-wg-12345678-peer-db.cmp-data-tunnel.svc.cluster.local",
    source_db_port=5432,
    tls_mode="disable",
    database="chat",
    db_auth_ref={"secret_name": "cmp-preflight-db", "username_key": "username",
                 "password_key": "password"},
    aws_vpc_cidrs=["10.0.0.0/16"],
    onprem_routed_cidrs=["10.42.0.0/16", "10.43.0.0/16"],
    tunnel_cidrs=["10.210.0.0/24"],
    expected_network_path={"allowed_private_destination_cidrs": ["10.43.0.0/16"]},
    probe_target={"agent_id": "office-seoul-01", "cluster_uid": "cluster-seoul",
                  "namespace": "chat"},
).model_dump(mode="json")  # fmt: skip
ONPREM = {**ARGS, "tunnel": TUNNEL, "network_preflight": PREFLIGHT}


class OnPremFakes(Fakes):
    def __init__(self, *, verdict="PASS", fingerprint=None, tunnel_fails=False,
                 tunnel_cleanup_fails=False, **kw):  # fmt: skip
        super().__init__(**kw)
        self.verdict, self.fingerprint = verdict, fingerprint
        self.tunnel_fails, self.tunnel_cleanup_fails = tunnel_fails, tunnel_cleanup_fails

    def activities(self):
        @activity.defn(name="dm_tunnel")
        async def tunnel(payload: dict) -> dict:
            self.calls.append("dm_tunnel")
            if self.tunnel_fails:
                raise ApplicationError("t", type="TUNNEL_HANDSHAKE_TIMEOUT", non_retryable=True)
            return {"name": "cmp-wg-x", "endpoint": "nlb.example", "handshake": True}

        @activity.defn(name="dm_tunnel_cleanup")
        async def tunnel_cleanup(payload: dict) -> dict:
            self.calls.append("dm_tunnel_cleanup")
            assert payload["tunnel"] == TUNNEL and payload["request"]["migration_id"]
            if self.tunnel_cleanup_fails:
                raise ApplicationError("c", type="TUNNEL_CLEANUP_FAILED", non_retryable=True)
            return {"name": "cmp-wg-x", "cleaned": True}

        @activity.defn(name="run_network_preflight")
        async def preflight(request: dict) -> dict:
            self.calls.append("run_network_preflight")
            parsed = NetworkPreflightRequest.model_validate(request)
            return {
                "status": self.verdict,
                "checks": [{"name": "check_cidr_overlap", "status": self.verdict}],
                "migration_id": parsed.migration_id,
                "tested_endpoint": f"{parsed.source_db_host}:5432",
                "probe_location": {"agent_id": "office-seoul-01", "cluster_uid": "cluster-seoul",
                                   "namespace": "chat", "node": None},
                "checked_at": datetime(2026, 11, 7, tzinfo=UTC).isoformat(),
                "input_fingerprint": self.fingerprint or input_fingerprint(parsed),
            }  # fmt: skip

        return [*super().activities(), tunnel, tunnel_cleanup, preflight]


def workers(env, fakes, **kw):
    return [
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[DataMigrationWorkflow], **kw),
        Worker(env.client, task_queue=MIGRATION_QUEUE, activities=fakes.activities()),
    ]


async def run(fakes, args=ONPREM, signals=()):
    """끝까지 돌리고 (결과, 이력)을 준다. `signals`는 streaming 뒤 차례로 보낼 신호 이름."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        ws = workers(env, fakes)
        async with ws[0], ws[1]:
            handle = await env.client.start_workflow(
                DataMigrationWorkflow.run, args, id=f"data-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )  # fmt: skip
            for name in signals:
                await wait_phase(handle, "streaming" if name != "rollback" else "switched")
                if name == "switchover":
                    await wait_announced(fakes)
                await handle.signal(name)
            return await handle.result(), await handle.fetch_history()


def ordered(fakes):
    return [c for c in fakes.calls if not c.startswith(("dm_sample", "dm_readiness",
                                                         "dm_announce"))]  # fmt: skip


async def test_pass_runs_tunnel_then_preflight_then_replication_and_cleans_up_after_finish():
    fakes = OnPremFakes()
    result, _ = await run(fakes, signals=["finish"])
    assert result["status"] == "done" and result["tunnel_cleanup_code"] is None
    calls = ordered(fakes)
    assert calls[:3] == ["dm_tunnel", "run_network_preflight", "dm_precheck"]
    assert calls[-2:] == ["dm_route_app", "dm_tunnel_cleanup"]
    # PgBouncer 설치는 준비·복사와 함께 돈다(순서 고정 없음)
    assert sorted(calls[3:-2]) == sorted(["dm_prepare", "dm_copy_schema", "dm_subscribe",
                                          "dm_install_cutover"])  # fmt: skip


@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
async def test_fail_or_unknown_blocks_before_replication_and_cleans_the_tunnel(verdict):
    fakes = OnPremFakes(verdict=verdict)
    result, _ = await run(fakes)
    assert result["status"] == "blocked"
    assert result["failure_code"] == f"NETWORK_PREFLIGHT_{verdict}"
    assert ordered(fakes) == ["dm_tunnel", "run_network_preflight", "dm_tunnel_cleanup"]


async def test_pass_for_another_input_is_not_reused():
    fakes = OnPremFakes(fingerprint="0" * 64)
    result, _ = await run(fakes)
    assert (result["status"], result["failure_code"]) == ("blocked", "PREFLIGHT_INPUT_CHANGED")
    assert "dm_precheck" not in fakes.calls and fakes.calls[-1] == "dm_tunnel_cleanup"


async def test_failed_replication_cleans_slots_first_then_the_tunnel():
    fakes = OnPremFakes(fail="dm_install_cutover")
    result, _ = await run(fakes)
    assert result["status"] == "failed" and result["failure_code"] == "DB_COMMAND_FAILED"
    assert fakes.calls[-2:] == ["dm_cleanup", "dm_tunnel_cleanup"]  # 슬롯 정리는 터널이 있어야 한다


async def test_failed_tunnel_still_removes_what_it_made():
    fakes = OnPremFakes(tunnel_fails=True)
    result, _ = await run(fakes)
    assert result["failure_code"] == "TUNNEL_HANDSHAKE_TIMEOUT"
    assert fakes.calls == ["dm_tunnel", "dm_tunnel_cleanup"]  # NLB가 남지 않게


async def test_cancel_during_streaming_cleans_slots_then_tunnel():
    fakes = OnPremFakes()
    result, _ = await run(fakes, signals=["request_cancel"])
    assert result["status"] == "cancelled"
    assert fakes.calls[-2:] == ["dm_cleanup", "dm_tunnel_cleanup"]


async def test_rollback_keeps_the_tunnel_until_finish():
    fakes = OnPremFakes()
    result, _ = await run(fakes, signals=["switchover", "rollback", "finish"])
    assert result["status"] == "done" and result["active"] == "source"
    calls = ordered(fakes)
    assert calls.count("dm_tunnel_cleanup") == 1 and calls[-1] == "dm_tunnel_cleanup"
    assert calls.index("dm_switchover:reverse:2") < calls.index("dm_tunnel_cleanup")


async def test_cleanup_failure_is_reported_without_hiding_the_result():
    fakes = OnPremFakes(verdict="FAIL", tunnel_cleanup_fails=True)
    result, _ = await run(fakes)
    assert result["failure_code"] == "NETWORK_PREFLIGHT_FAIL"
    assert result["tunnel_cleanup_code"] == "TUNNEL_CLEANUP_FAILED"


async def test_without_preflight_input_the_gate_is_not_run():
    fakes = OnPremFakes()
    result, _ = await run(fakes, args={**ARGS, "tunnel": TUNNEL}, signals=["finish"])
    assert result["status"] == "done" and "run_network_preflight" not in fakes.calls
    assert fakes.calls[-1] == "dm_tunnel_cleanup"


async def test_without_tunnel_nothing_is_cleaned():
    fakes = OnPremFakes()
    result, _ = await run(fakes, args=ARGS, signals=["finish"])
    assert result["status"] == "done" and "dm_tunnel_cleanup" not in fakes.calls


async def test_history_from_before_the_patches_replays(monkeypatch):
    """패치 전 코드로 만든 이력(사전 점검·터널 정리 없음)을 새 코드가 그대로 재생한다."""
    real = workflow.patched
    new = {dmw.PREFLIGHT_PATCH, dmw.TUNNEL_CLEANUP_PATCH}
    monkeypatch.setattr(workflow, "patched", lambda pid: False if pid in new else real(pid))
    fakes = OnPremFakes()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        ws = workers(env, fakes, workflow_runner=UnsandboxedWorkflowRunner())
        async with ws[0], ws[1]:
            handle = await env.client.start_workflow(
                DataMigrationWorkflow.run, ONPREM, id=f"data-{uuid.uuid4()}",
                task_queue=WORKFLOW_QUEUE,
            )  # fmt: skip
            await wait_phase(handle, "streaming")
            await handle.signal("finish")
            await handle.result()
            history = await handle.fetch_history()
    assert "run_network_preflight" not in fakes.calls and "dm_tunnel_cleanup" not in fakes.calls
    monkeypatch.setattr(workflow, "patched", real)
    await Replayer(workflows=[DataMigrationWorkflow]).replay_workflow(history)


async def test_new_history_replays():
    fakes = OnPremFakes()
    _, history = await run(fakes, signals=["finish"])
    await Replayer(workflows=[DataMigrationWorkflow]).replay_workflow(history)
