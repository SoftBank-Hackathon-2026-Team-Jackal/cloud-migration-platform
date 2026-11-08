"""데이터 이전(CDC) Activity(specs/data-migration, ticket 02·03).

DB에 직접 접속하지 않고, kubeconfig로 DB Pod 안에서 psql을 실행한다(DM-001; Provisioning
`database_probe`와 같은 방식). Pod 안에서는 유닉스 소켓으로 접속하므로 비밀번호가 필요 없다.
구독자가 발행 DB에 접속할 때 쓰는 전용 역할(`cmp_repl`)의 비밀번호만 Worker가 만들어 구독자 쪽
Kubernetes Secret에 보관하고, SQL은 stdin으로만 넘긴다(명령줄·Workflow 입력/결과·로그에 없음).

방향: forward는 원본이 발행자·대상이 구독자, reverse(전환 뒤 롤백 대비)는 반대다.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import json
import os
import secrets
import shlex
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from cmp_data import (
    DataMigrationRequest,
    DbEndpoint,
    evaluate_precheck,
    evaluate_readiness,
    progress_event,
    readiness_event,
    replication_names,
    sync_events,
    sync_phase,
)
from cmp_data import sql as q
from temporalio import activity
from temporalio.exceptions import ApplicationError

from cmp_worker.activities.k8s_workloads_cli import CommandError, CommandRunner, KubectlSession


# cmp_worker.events는 cmp_api 설정을 읽으므로 import 시점이 아니라 호출할 때 불러온다
# (이 모듈은 Worker 등록 때 Workflow 샌드박스 검사 경로에서도 import된다).
async def emit_event_async(*args, **kwargs):
    from cmp_worker.events import emit_event_async as emit

    return await emit(*args, **kwargs)


def log_event(*args, **kwargs):
    from cmp_worker.events import log_event as log

    return log(*args, **kwargs)


PHASE = "data_copy"
SECRET_KEY = "password"
PSQL = (
    f"PGAPPNAME={q.TOOL_APP_NAME} exec psql -X -q -At -v ON_ERROR_STOP=1 "
    '-U "$POSTGRES_USER" -d "$POSTGRES_DB"'
)
# 스키마 복원은 한 트랜잭션: 중간에 실패해도 표가 반쯤 남지 않는다(남으면 재시도가 TARGET_SCHEMA_PARTIAL로 멈춘다).
PSQL_ONE_TX = PSQL + " --single-transaction"
PG_DUMP_SCHEMA = (
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only --no-owner '
    "--no-privileges --no-publications --no-subscriptions"
)


class DbPod(Protocol):
    """DB Pod 하나. 오류는 코드만 담은 `CommandError`로 올라온다(출력은 버린다)."""

    def query(self, sql: str) -> str: ...
    def execute(self, sql: str) -> None: ...
    def dump_schema(self, exclude: frozenset[str] = frozenset()) -> bytes: ...
    def restore(self, script: bytes) -> None: ...
    def restart(self, timeout: int = 180) -> None: ...
    def read_secret(self, name: str) -> str | None: ...
    def create_secret(self, name: str, value: str) -> None: ...
    def login(self) -> tuple[str, str]: ...


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

    def dump_schema(self, exclude: frozenset[str] = frozenset()) -> bytes:
        flags = "".join(f" --exclude-table={shlex.quote(t)}" for t in sorted(exclude))
        return self._exec(PG_DUMP_SCHEMA + flags, timeout=300)

    def restore(self, script: bytes) -> None:
        self._exec(PSQL_ONE_TX, script, timeout=300)

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

    def login(self) -> tuple[str, str]:
        """DB 컨테이너의 관리자 로그인(POSTGRES_USER/POSTGRES_PASSWORD). 메모리에만 둔다."""
        out = self._exec('printf "%s\\n%s" "$POSTGRES_USER" "$POSTGRES_PASSWORD"').decode()
        user, _, password = out.partition("\n")
        if not user or not password:
            raise CommandError("kubectl")
        return user, password

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


class EksSessionCache:
    """EKS kubeconfig with a static bearer token, reused across Activities in this process.

    `aws eks update-kubeconfig` writes an `exec` credential plugin, so every `kubectl` call ran
    `aws eks get-token` again (about 1 s each, measured on the demo worker; a k3s call takes
    0.1 s). A data migration makes dozens of calls on the source side, so this writes the cluster
    endpoint and one token instead and refreshes them well before the token expires (EKS tokens
    last 15 minutes). The file is 0600 in a 0700 directory, replaced atomically (a `kubectl` that
    already read the old one keeps a still-valid token), and removed at process exit. The token
    never enters Workflow input/output or logs."""

    # A token must outlive the longest single kubectl call that starts with it: `rollout status`
    # waits up to 330 s (data_cutover). aws-cli tokens last 14 minutes, so one is reused ~8 min.
    REFRESH_BEFORE_SECONDS = 360

    def __init__(self, runtime_factory=None, clock=time.time):
        self._factory = runtime_factory or self._cli_runtime
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple, tuple[float, Path, CommandRunner]] = {}
        self._root: Path | None = None

    @staticmethod
    def _cli_runtime(profile: str | None) -> CommandRunner:
        from cmp_worker.activities.aws_workloads_cli import CliRuntime

        return CliRuntime(aws_profile=profile)

    def _dir(self) -> Path:
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="cmp-eks-cache-"))
            self._root.chmod(0o700)
            atexit.register(shutil.rmtree, self._root, True)
        return self._root

    def _refresh(self, key: tuple, runtime: CommandRunner) -> tuple[float, Path]:
        cluster, region, _ = key
        aws = getattr(runtime, "aws", "aws")
        describe = [aws, "eks", "describe-cluster", "--name", cluster, "--region", region,
                    "--query", "cluster.{endpoint:endpoint,ca:certificateAuthority.data}",
                    "--output", "json"]  # fmt: skip
        token = [aws, "eks", "get-token", "--cluster-name", cluster, "--region", region,
                 "--output", "json"]  # fmt: skip
        info = json.loads(runtime.run(describe))
        cred = json.loads(runtime.run(token))["status"]
        expires = datetime.fromisoformat(cred["expirationTimestamp"])
        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "eks",
                    "cluster": {
                        "server": info["endpoint"],
                        "certificate-authority-data": info["ca"],
                    },
                }
            ],
            "users": [{"name": "eks", "user": {"token": cred["token"]}}],
            "contexts": [{"name": "eks", "context": {"cluster": "eks", "user": "eks"}}],
            "current-context": "eks",
        }
        folder = self._dir()
        path = folder / (hashlib.sha256(repr(key).encode()).hexdigest()[:16] + ".json")
        fd, tmp = tempfile.mkstemp(dir=folder)
        with os.fdopen(fd, "w") as f:
            json.dump(kubeconfig, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return expires.timestamp() - self.REFRESH_BEFORE_SECONDS, path

    @contextmanager
    def connect(self, endpoint: DbEndpoint) -> Iterator[KubectlSession]:
        profile = os.environ.get("DATA_MIGRATION_AWS_PROFILE")
        key = (endpoint.cluster_name, endpoint.region, profile)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or self._clock() >= entry[0]:
                runtime = entry[2] if entry else self._factory(profile)
                try:
                    good_until, path = self._refresh(key, runtime)
                except (KeyError, TypeError, ValueError):
                    raise CommandError("aws") from None
                entry = (good_until, path, runtime)
                self._entries[key] = entry
        _, path, runtime = entry
        try:
            with tempfile.TemporaryDirectory(prefix="cmp-eks-") as workdir:  # per-call scratch
                yield KubectlSession(runtime, path, Path(workdir))
        except CommandError:
            # Errors carry no output, so a rejected token looks like any other failure. Drop the
            # entry; the Activity retry then starts from a fresh token (one extra ~3 s refresh).
            with self._lock:
                if self._entries.get(key) is entry:
                    del self._entries[key]
            raise


EKS_SESSIONS = EksSessionCache()


def _runtime_for(endpoint: DbEndpoint):
    if endpoint.access == "AWS_EKS":
        return EKS_SESSIONS.connect(endpoint)
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
    건너뛴다. 일부만 있으면(대상 앱이 시작하며 자기 표를 먼저 만든 경우) 그 표의 컬럼(이름·타입)이
    원본과 같을 때만 그 표와 소유 시퀀스를 덤프에서 빼고 나머지를 복원한다. 다르면 사람이 봐야 하므로
    멈춘다. 새 표가 이미 있는 표를 참조하는 외래키는 한 트랜잭션 복원 안에서 그대로 걸린다.

    비교·복사는 사용자 표만 본다. prepare가 발행자에 만든 marker 표는 빼고, 구독자에는 따로 만든다
    (`for all tables` 발행이 marker 행도 보내므로 구독자에 표가 있어야 한다, Provisioning 72)."""
    publisher, subscriber = _sides(req)
    skipped: list[str] = []
    with OPEN_DB(publisher) as src, OPEN_DB(subscriber) as dst:
        wanted = q.parse_user_tables(src.query(q.USER_TABLES_SQL))
        present = q.parse_user_tables(dst.query(q.USER_TABLES_SQL))
        copied = not wanted <= present
        if copied:
            exclude = set(q.INTERNAL_DUMP_EXCLUDES)
            existing = present & wanted
            if existing:
                ours = q.parse_table_shapes(src.query(q.table_shapes_sql(existing)))
                theirs = q.parse_table_shapes(dst.query(q.table_shapes_sql(existing)))
                if any(
                    t not in ours or t not in theirs or ours[t]["columns"] != theirs[t]["columns"]
                    for t in existing
                ):
                    raise _fail("TARGET_SCHEMA_PARTIAL", retryable=False)
                # 덤프에 나오는 이름은 원본 것이다. 대상 이름도 함께 빼 두면 해가 없다.
                for t in existing:
                    exclude |= {t} | ours[t]["sequences"] | theirs[t]["sequences"]
                skipped = sorted(existing)
            dst.restore(src.dump_schema(frozenset(exclude)))
        dst.execute(q.ENSURE_MARKER_SQL)
    return {"copied": copied, "tables": len(wanted), "skipped": skipped}


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
    """복제 상태 한 번 읽기 → data.phase·data.lsn·data.table·data.progress. 반복은 Workflow 타이머가
    맡는다. data.progress는 같은 샘플로 계산한다(DB 조회를 늘리지 않는다)."""
    req = DataMigrationRequest.model_validate(payload)
    snap = await _run(sample, req)
    for event_type, data in sync_events(snap, req.policy):
        await emit_event_async(req.migration_id, event_type, PHASE, "running", data)
    progress = progress_event(snap, req.policy)
    # 샘플(Activity 실행)마다 한 줄. 같은 시도 안 재발행은 단계·행 수 키로 한 줄로 남는다.
    await emit_event_async(
        req.migration_id, "data.progress", PHASE,
        "done" if progress["step"] == "ready" else "running", progress,
        idempotency_key=f"progress:{progress['step']}:{progress['rows']}",
    )  # fmt: skip
    return {
        "phase": sync_phase(snap, req.policy),
        "lag_bytes": snap.lag_bytes,
        "lag_ms": snap.lag_ms,  # #234: traffic.ready reads it from the `status` Query
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
