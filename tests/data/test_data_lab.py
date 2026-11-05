"""실제 PostgreSQL 16 두 개(k3d 클러스터 두 개)로 하는 데이터 이전 시험(ticket 02·03).

준비: `infra/local/data-lab/README.md`. `DATA_MIGRATION_KUBECONFIG_DIR`가 없으면 건너뛴다.
대상 DB의 public 스키마를 지우고 시작하므로 실습 클러스터에서만 돌린다.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import validate_data
from cmp_data import DataMigrationRequest
from cmp_worker.activities import data_migration as dm
from cmp_worker.events import configure_emitter
from temporalio.testing import ActivityEnvironment

KC_DIR = os.environ.get("DATA_MIGRATION_KUBECONFIG_DIR")
SRC_HOST = os.environ.get("DATA_LAB_SRC_HOST", "")
DST_HOST = os.environ.get("DATA_LAB_DST_HOST", "")
pytestmark = pytest.mark.skipif(
    not (KC_DIR and SRC_HOST and DST_HOST),
    reason="needs the data-lab k3d clusters (DATA_MIGRATION_KUBECONFIG_DIR, DATA_LAB_*_HOST)",
)


def psql(ref: str, sql: str) -> str:
    done = subprocess.run(
        ["kubectl", "--kubeconfig", f"{KC_DIR}/{ref}.yaml", "-n", "data-lab", "exec", "-i",
         "db-0", "-c", "postgres", "--", "psql", "-U", "chat", "-d", "chat", "-XAt",
         "-v", "ON_ERROR_STOP=1"],
        input=sql.encode(), capture_output=True, check=True, timeout=60,
    )  # fmt: skip
    return done.stdout.decode().strip()


FINGERPRINT = """select (select count(*) from messages) || '|' || (select count(*) from audit_log)
  || '|' || (select md5(string_agg(id::text || body, ',' order by id)) from messages)"""


@pytest.fixture
def request_payload():
    def ep(ref, host):
        return {"access": "KUBECONFIG", "kubeconfig_ref": ref, "namespace": "data-lab",
                "pod": "db-0", "peer_host": host, "peer_port": 30432, "dbname": "chat"}  # fmt: skip

    payload = {
        "migration_id": f"migration-{uuid.uuid4()}",
        "source": ep("cmp-dm-src", SRC_HOST),
        "target": ep("cmp-dm-dst", DST_HOST),
    }
    # 지난 실행이 남긴 구독·발행을 지우고 대상 스키마를 비운다.
    for sub in psql("cmp-dm-dst", "select subname from pg_subscription").split():
        psql("cmp-dm-dst", f"alter subscription {sub} disable; "
                           f"alter subscription {sub} set (slot_name = none); drop subscription {sub};")  # fmt: skip
    for slot in psql("cmp-dm-src", "select slot_name from pg_replication_slots").split():
        psql("cmp-dm-src", f"select pg_drop_replication_slot('{slot}')")
    for pub in psql("cmp-dm-src", "select pubname from pg_publication").split():
        psql("cmp-dm-src", f"drop publication {pub}")
    psql("cmp-dm-dst", "drop schema public cascade; create schema public;")
    yield payload
    for sub in psql("cmp-dm-dst", "select subname from pg_subscription").split():
        psql("cmp-dm-dst", f"drop subscription {sub}")


@pytest.fixture
def emitter():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


async def test_initial_copy_and_cdc_reach_streaming_with_matching_data(
    request_payload, emitter
) -> None:
    env = ActivityEnvironment()
    pre = await env.run(dm.dm_precheck, request_payload)
    assert pre["ok"], pre
    await env.run(dm.dm_prepare, {"request": request_payload, "actions": pre["actions"]})
    assert (await env.run(dm.dm_copy_schema, request_payload))["copied"]
    assert (await env.run(dm.dm_subscribe, request_payload))["created_subscription"]

    phase = None
    for _ in range(60):
        phase = (await env.run(dm.dm_sample, request_payload))["phase"]
        if phase == "streaming":
            break
        await asyncio.sleep(1)
    assert phase == "streaming"

    # CDC: 쓰기·수정·삭제(PK 없는 audit_log 포함)가 대상에 반영된다.
    psql("cmp-dm-src", "insert into messages(room_id, body) select 1, 'lab-' || g "
                       "from generate_series(1, 300) g; update messages set body = 'lab-edit' "
                       "where id % 97 = 0; delete from audit_log where ctid in "
                       "(select ctid from audit_log limit 1);")  # fmt: skip
    for _ in range(30):
        if psql("cmp-dm-src", FINGERPRINT) == psql("cmp-dm-dst", FINGERPRINT):
            break
        await asyncio.sleep(1)
    assert psql("cmp-dm-src", FINGERPRINT) == psql("cmp-dm-dst", FINGERPRINT)

    ready = await env.run(dm.dm_readiness, request_payload)
    assert ready["ready"], ready

    types = {e.type for e in emitter.events}
    assert {"data.phase", "data.lsn", "data.table", "data.readiness"} <= types
    for event in emitter.events:
        validate_data(event.type, event.data)

    await env.run(dm.dm_cleanup, request_payload)
    assert psql("cmp-dm-src", "select count(*) from pg_replication_slots") == "0"

    DataMigrationRequest.model_validate(request_payload)  # 입력에 비밀값 필드가 없다
