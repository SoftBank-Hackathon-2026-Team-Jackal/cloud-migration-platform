"""데이터 이전(CDC) Activity(specs/data-migration, ticket 02·03).

DB에 직접 접속하지 않고, kubeconfig로 DB Pod 안에서 psql을 실행한다(DM-001; Provisioning
`database_probe`와 같은 방식). Pod 안에서는 유닉스 소켓으로 접속하므로 비밀번호가 필요 없다.
구독자가 발행 DB에 접속할 때 쓰는 전용 역할(`cmp_repl`)의 비밀번호만 Worker가 만들어 구독자 쪽
Kubernetes Secret에 보관하고, SQL은 stdin으로만 넘긴다(명령줄·Workflow 입력/결과·로그에 없음).

방향: forward는 원본이 발행자·대상이 구독자, reverse(전환 뒤 롤백 대비)는 반대다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

from cmp_data import (
    DataMigrationRequest,
    DbEndpoint,
    evaluate_precheck,
    evaluate_readiness,
    readiness_event,
    replication_names,
    sync_events,
    sync_phase,
)
from cmp_data import sql as q
from temporalio import activity
from temporalio.exceptions import ApplicationError

from cmp_worker.activities.k8s_workloads_cli import CommandError, CommandRunner, KubectlSession
from cmp_worker.events import emit_event_async, log_event

PHASE = "data_copy"
SECRET_KEY = "password"
PSQL = (
    f"PGAPPNAME={q.TOOL_APP_NAME} exec psql -X -q -At -v ON_ERROR_STOP=1 "
    '-U "$POSTGRES_USER" -d "$POSTGRES_DB"'
)
PG_DUMP_SCHEMA = (
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only --no-owner '
    "--no-privileges --no-publications --no-subscriptions"
)


class DbPod(Protocol):
    """DB Pod 하나. 오류는 코드만 담은 `CommandError`로 올라온다(출력은 버린다)."""

    def query(self, sql: str) -> str: ...
    def execute(self, sql: str) -> None: ...
    def dump_schema(self) -> bytes: ...
    def restore(self, script: bytes) -> None: ...
    def restart(self, timeout: int = 180) -> None: ...
    def read_secret(self, name: str) -> str | None: ...
    def create_secret(self, name: str, value: str) -> None: ...


class KubectlDbPod:
    def __init__(self, session: KubectlSession, endpoint: DbEndpoint):
        self._s, self._ep = session, endpoint

    def _exec(self, script: str, stdin: bytes | None = None, timeout: int = 120) -> bytes:
        ep = self._ep
        return self._s._kubectl(
            "exec", "-i", "-n", ep.namespace, ep.pod, "-c", ep.container, "--",
            "sh", "-c", script,
            stdin=stdin, timeout=timeout,
        )  # fmt: skip

    def query(self, sql: str) -> str:
        return self._exec(PSQL, sql.encode()).decode().strip()

    def execute(self, sql: str) -> None:
        self._exec(PSQL, sql.encode())

    def dump_schema(self) -> bytes:
        return self._exec(PG_DUMP_SCHEMA, timeout=300)

    def restore(self, script: bytes) -> None:
        self._exec(PSQL, script, timeout=300)

    def restart(self, timeout: int = 180) -> None:
        ep = self._ep
        self._s._kubectl("delete", "pod", "-n", ep.namespace, ep.pod, "--wait=true", timeout=120)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._s._kubectl(
                    "wait", "-n", ep.namespace, f"pod/{ep.pod}", "--for=condition=Ready",
                    "--timeout=10s", timeout=30,
                )  # fmt: skip
                return
            except CommandError:
                time.sleep(2)  # StatefulSet이 같은 이름의 Pod를 다시 만들 때까지
        raise CommandError("kubectl")

    def read_secret(self, name: str) -> str | None:
        item = self._s._get("secret", name, self._ep.namespace)
        if not item:
            return None
        raw = (item.get("data") or {}).get(SECRET_KEY)
        return base64.b64decode(raw).decode() if raw else None

    def create_secret(self, name: str, value: str) -> None:
        # `kubectl create -f -`: 값이 last-applied 주석에 복사되지 않는다(k8s_workloads_cli 규칙).
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": self._ep.namespace,
                "labels": {"app.kubernetes.io/managed-by": "cmp-data-migration"},
            },
            "type": "Opaque",
            "stringData": {SECRET_KEY: value},
        }
        self._s._kubectl("create", "-f", "-", stdin=json.dumps(manifest).encode())


def _runtime_for(endpoint: DbEndpoint):
    if endpoint.access == "AWS_EKS":
        from cmp_worker.activities.aws_workloads_cli import CliRuntime

        runtime = CliRuntime(aws_profile=os.environ.get("DATA_MIGRATION_AWS_PROFILE"))
        target = SimpleNamespace(cluster_name=endpoint.cluster_name, region=endpoint.region)
        return runtime.connect(target)
    if endpoint.access == "GCP_GKE":
        from cmp_worker.activities.gcp_workloads import GkeTarget
        from cmp_worker.activities.gcp_workloads_cli import GcpCliRuntime

        runtime = GcpCliRuntime(
            gcloud_configuration=os.environ.get("DATA_MIGRATION_GCLOUD_CONFIGURATION")
        )
        return runtime.connect(
            GkeTarget(
                project_id=endpoint.project_id or "",
                cluster_name=endpoint.cluster_name or "",
                location=endpoint.location or "",
            )
        )
    return _kubeconfig_session(endpoint)


@contextmanager
def _kubeconfig_session(endpoint: DbEndpoint) -> Iterator[KubectlSession]:
    root = os.environ.get("DATA_MIGRATION_KUBECONFIG_DIR")
    ref = endpoint.kubeconfig_ref
    if not root or not ref or ref.startswith("."):
        raise ApplicationError(
            "kubeconfig is not configured", type="DbAccessNotConfigured", non_retryable=True
        )
    path = Path(root) / f"{ref}.yaml"
    if not path.is_file():
        raise ApplicationError(
            "kubeconfig is not configured", type="DbAccessNotConfigured", non_retryable=True
        )
    yield KubectlSession(CommandRunner(), path, path.parent)


@contextmanager
def open_db(endpoint: DbEndpoint) -> Iterator[DbPod]:
    with _runtime_for(endpoint) as session:
        yield KubectlDbPod(session, endpoint)


# 시험에서 가짜 DbPod로 바꾼다.
OPEN_DB = open_db


def _sides(req: DataMigrationRequest) -> tuple[DbEndpoint, DbEndpoint]:
    """(발행자, 구독자)."""
    if req.direction == "forward":
        return req.source, req.target
    return req.target, req.source


def _secret_name(req: DataMigrationRequest) -> str:
    return replication_names(req.migration_id, req.direction).subscription.replace("_", "-")


def _fail(code: str, *, retryable: bool = True) -> ApplicationError:
    return ApplicationError(code, type=code, non_retryable=not retryable)


# ---- 동기 본문(스레드에서 실행) ----


def precheck(req: DataMigrationRequest) -> dict:
    publisher, _ = _sides(req)
    with OPEN_DB(publisher) as db:
        facts = q.parse_precheck(db.query(q.PRECHECK_SQL))
    result = evaluate_precheck(facts)
    return {**result.model_dump(mode="json"), "sequences": facts.sequences}


def prepare(req: DataMigrationRequest, actions: list[list[str]]) -> dict:
    """발행자: REPLICA IDENTITY FULL·marker 테이블. 구독자: 역방향 발행을 위해 wal_level=logical."""
    publisher, subscriber = _sides(req)
    tables = [target for code, target in actions if code == "REPLICA_IDENTITY_FULL"]
    with OPEN_DB(publisher) as db:
        db.execute(q.replica_identity_full_sql(tables) + q.ENSURE_MARKER_SQL)
    restarted = False
    with OPEN_DB(subscriber) as db:
        if db.query(q.WAL_LEVEL_SQL) != "logical":
            db.execute(q.SET_WAL_LOGICAL_SQL)
            db.restart()
            restarted = True
            if db.query(q.WAL_LEVEL_SQL) != "logical":
                raise _fail("TARGET_WAL_LEVEL_NOT_LOGICAL", retryable=False)
    return {"replica_identity_full": tables, "subscriber_restarted": restarted}


def copy_schema(req: DataMigrationRequest) -> dict:
    """스키마만 복사한다(논리 복제는 DDL을 옮기지 않는다). 구독자에 이미 같은 테이블이 다 있으면
    건너뛰고, 일부만 있으면 사람이 봐야 하므로 멈춘다."""
    publisher, subscriber = _sides(req)
    with OPEN_DB(publisher) as src, OPEN_DB(subscriber) as dst:
        wanted = set(json.loads(src.query(q.USER_TABLES_SQL)))
        present = set(json.loads(dst.query(q.USER_TABLES_SQL)))
        if wanted <= present:
            return {"copied": False, "tables": len(wanted)}
        if present & wanted:
            raise _fail("TARGET_SCHEMA_PARTIAL", retryable=False)
        dst.restore(src.dump_schema())
    return {"copied": True, "tables": len(wanted)}


def subscribe(req: DataMigrationRequest, copy_data: bool = True) -> dict:
    names = replication_names(req.migration_id, req.direction)
    publisher, subscriber = _sides(req)
    secret = _secret_name(req)
    with OPEN_DB(publisher) as pub, OPEN_DB(subscriber) as sub:
        password = sub.read_secret(secret)
        if password is None:
            password = secrets.token_urlsafe(24)
            sub.create_secret(secret, password)
        pub.execute(q.ensure_repl_role_sql(password))
        created_pub = pub.query(q.publication_exists_sql(names)) == "0"
        if created_pub:
            pub.execute(q.create_publication_sql(names))
        created_sub = sub.query(q.subscription_exists_sql(names)) == "0"
        if created_sub:
            sub.execute(
                q.create_subscription_sql(
                    names,
                    host=publisher.peer_host,
                    port=publisher.peer_port,
                    dbname=publisher.dbname,
                    password=password,
                    copy_data=copy_data,
                )
            )
    return {
        "publication": names.publication,
        "subscription": names.subscription,
        "created_publication": created_pub,
        "created_subscription": created_sub,
    }


def sample(req: DataMigrationRequest):
    names = replication_names(req.migration_id, req.direction)
    publisher, subscriber = _sides(req)
    with OPEN_DB(publisher) as pub, OPEN_DB(subscriber) as sub:
        target_raw = sub.query(q.target_sync_sql(names))
        source_raw = pub.query(q.source_sync_sql(names))
    return q.parse_sync(target_raw, source_raw)


def readiness(req: DataMigrationRequest, window: float = 1.0):
    names = replication_names(req.migration_id, req.direction)
    publisher, _ = _sides(req)
    with OPEN_DB(publisher) as db:
        first = db.query(q.readiness_sql(names))
        started = time.monotonic()
        time.sleep(window)
        second = db.query(q.readiness_sql(names))
        return q.parse_readiness(first, second, time.monotonic() - started)


def cleanup(req: DataMigrationRequest) -> dict:
    """구독(원본 슬롯 포함)과 발행을 지운다. 슬롯이 남으면 원본 WAL이 계속 쌓인다."""
    names = replication_names(req.migration_id, req.direction)
    publisher, subscriber = _sides(req)
    with OPEN_DB(subscriber) as sub:
        sub.execute(q.drop_subscription_sql(names))
    with OPEN_DB(publisher) as pub:
        pub.execute(q.drop_publication_sql(names))
    return {"dropped": names.subscription}


# ---- Activity ----


async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except CommandError as exc:
        # 출력에는 DB 데이터가 섞일 수 있어 도구 이름만 남긴다.
        raise _fail("DB_COMMAND_FAILED") from exc


async def _phase(req: DataMigrationRequest, phase: str) -> None:
    await emit_event_async(req.migration_id, "data.phase", PHASE, "running", {"phase": phase})


@activity.defn
async def dm_precheck(payload: dict) -> dict:
    req = DataMigrationRequest.model_validate(payload)
    await _phase(req, "preflight")
    result = await _run(precheck, req)
    if not result["ok"]:
        await _phase(req, "blocked")
        log_event(req.migration_id, "error", "DATA_PRECHECK_BLOCKED", {"codes": result["blockers"]})
    elif result["warnings"]:
        log_event(req.migration_id, "warning", "DATA_PRECHECK_WARN", {"codes": result["warnings"]})
    return result


@activity.defn
async def dm_prepare(payload: dict) -> dict:
    req = DataMigrationRequest.model_validate(payload["request"])
    return await _run(prepare, req, payload.get("actions") or [])


@activity.defn
async def dm_copy_schema(payload: dict) -> dict:
    req = DataMigrationRequest.model_validate(payload)
    result = await _run(copy_schema, req)
    await _phase(req, "schema")
    return result


@activity.defn
async def dm_subscribe(payload: dict) -> dict:
    req = DataMigrationRequest.model_validate(payload)
    return await _run(subscribe, req)


@activity.defn
async def dm_sample(payload: dict) -> dict:
    """복제 상태 한 번 읽기 → data.phase·data.lsn·data.table. 반복은 Workflow 타이머가 맡는다."""
    req = DataMigrationRequest.model_validate(payload)
    snap = await _run(sample, req)
    for event_type, data in sync_events(snap, req.policy):
        await emit_event_async(req.migration_id, event_type, PHASE, "running", data)
    return {
        "phase": sync_phase(snap, req.policy),
        "lag_bytes": snap.lag_bytes,
        "tables": len(snap.tables),
        "tables_ready": sum(1 for t in snap.tables if t.state == "r"),
    }


@activity.defn
async def dm_readiness(payload: dict) -> dict:
    req = DataMigrationRequest.model_validate(payload)
    snap = await _run(readiness, req)
    await emit_event_async(
        req.migration_id, "data.readiness", PHASE, "running", readiness_event(snap, req.policy)
    )
    ready, reasons = evaluate_readiness(snap, req.policy)
    return {"ready": ready, "reasons": reasons, **snap.model_dump(mode="json")}


@activity.defn
async def dm_cleanup(payload: dict) -> dict:
    req = DataMigrationRequest.model_validate(payload)
    return await _run(cleanup, req)
