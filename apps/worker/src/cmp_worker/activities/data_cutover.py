"""DB 전환 Activity(DM-010, ticket 06·04): 원본 클러스터에 PgBouncer + 전환 실행기(`cmp-cutover`)를
두고, 원본 앱을 PgBouncer로 연결한 뒤, PoC fast 순서로 쓰기 주체를 바꾼다. 롤백은 같은 실행기를
반대 방향으로 실행한다.

전환은 밀리초 단위라 Worker에서 SQL마다 `kubectl exec`를 하지 않는다. 실행기는 cmp-cutover Pod 안에서
접속을 유지한 채 한 번에 돌고, Activity는 stdin으로 설정을 넘긴 뒤 결과 JSON만 받는다.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from cmp_data import DataMigrationRequest, DbEndpoint, replication_names
from cmp_data import cutover as co
from cmp_data import sql as q
from temporalio import activity
from temporalio.exceptions import ApplicationError

from cmp_worker.activities import data_migration as dm
from cmp_worker.activities.k8s_workloads_cli import CommandError, KubectlSession
from cmp_worker.events import emit_event_async, log_event
from cmp_worker.poc_bridge import switchover_events

PHASE = "verification"  # 웹은 DB 전환(cutover.*)을 verification 단계에 둔다(poc_bridge와 같음)
RC_MARK = "__cmp_rc="


class ClusterPort(Protocol):
    def secret_data(self, namespace: str, name: str) -> dict[str, str] | None: ...
    def put_secret(
        self, namespace: str, name: str, data: dict[str, str], *, replace: bool
    ) -> None: ...
    def apply(self, manifests: list[dict]) -> None: ...
    def rollout_restart(self, namespace: str, deployment: str) -> None: ...
    def rollout_wait(self, namespace: str, deployment: str, timeout: int = 300) -> None: ...
    def pod_for(self, namespace: str, selector: str) -> str: ...
    def run_runner(self, namespace: str, pod: str, config: dict) -> tuple[int, dict]: ...


class KubectlCluster:
    def __init__(self, session: KubectlSession):
        self._s = session

    def secret_data(self, namespace: str, name: str) -> dict[str, str] | None:
        import base64

        item = self._s._get("secret", name, namespace)
        if not item:
            return None
        return {k: base64.b64decode(v).decode() for k, v in (item.get("data") or {}).items()}

    def put_secret(self, namespace: str, name: str, data: dict[str, str], *, replace: bool) -> None:
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace, "labels": co.LABELS},
            "type": "Opaque",
            "stringData": data,
        }
        # create/replace는 last-applied 주석에 값을 복사하지 않는다.
        verb = "replace" if replace else "create"
        self._s._kubectl(verb, "-f", "-", stdin=json.dumps(manifest).encode())

    def apply(self, manifests: list[dict]) -> None:
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": manifests}).encode()
        self._s._kubectl(
            "apply", "--server-side", "--field-manager", "cmp-data-migration", "-f", "-",
            stdin=body,
        )  # fmt: skip

    def rollout_restart(self, namespace: str, deployment: str) -> None:
        self._s._kubectl("rollout", "restart", "-n", namespace, f"deployment/{deployment}")

    def rollout_wait(self, namespace: str, deployment: str, timeout: int = 300) -> None:
        self._s._kubectl(
            "rollout", "status", "-n", namespace, f"deployment/{deployment}",
            f"--timeout={timeout}s", timeout=timeout + 30,
        )  # fmt: skip

    def pod_for(self, namespace: str, selector: str) -> str:
        out = self._s._kubectl(
            "get", "pod", "-n", namespace, "-l", selector,
            "--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].metadata.name}",
        )  # fmt: skip
        name = out.decode().strip()
        if not name:
            raise CommandError("kubectl")
        return name

    def run_runner(self, namespace: str, pod: str, config: dict) -> tuple[int, dict]:
        # 실행기의 종료 코드를 stdout 끝줄로 받는다(0이 아니어도 결과 JSON을 읽기 위해).
        script = f'python3 /opt/cmp/switchover.py; echo "{RC_MARK}$?"'
        out = self._s._kubectl(
            "exec", "-i", "-n", namespace, pod, "-c", "runner", "--", "sh", "-c", script,
            stdin=json.dumps(config).encode(), timeout=180,
        ).decode()  # fmt: skip
        lines = [line for line in out.splitlines() if line.strip()]
        rc = int(lines[-1].removeprefix(RC_MARK)) if lines and lines[-1].startswith(RC_MARK) else -1
        result = json.loads(lines[-2]) if len(lines) >= 2 else {}
        return rc, result


@contextmanager
def open_cluster(endpoint: DbEndpoint) -> Iterator[ClusterPort]:
    with dm._runtime_for(endpoint) as session:
        yield KubectlCluster(session)


OPEN_CLUSTER = open_cluster  # 시험에서 가짜로 바꾼다


def _source_host(ep: DbEndpoint) -> tuple[str, int]:
    return (ep.local_host or ep.peer_host, ep.local_port if ep.local_host else ep.peer_port)


def _fail(code: str, *, retryable: bool = False) -> ApplicationError:
    return ApplicationError(code, type=code, non_retryable=not retryable)


# ---- 동기 본문 ----


def install(req: DataMigrationRequest, app: co.SourceApp, settings: co.CutoverSettings) -> dict:
    """원본 앱 Namespace에 cmp-cutover(PgBouncer + 실행기)를 둔다. 연결 대상은 원본 DB."""
    with dm.OPEN_DB(req.source) as db:
        user, password = db.login()
    with OPEN_CLUSTER(req.source) as cluster:
        app_secret = cluster.secret_data(app.namespace, app.url_secret) or {}
        current = cluster.secret_data(app.namespace, co.SECRET)
        if current is None:
            original = app_secret.get(app.url_key) or ""
            if not original:
                raise _fail("APP_DATABASE_URL_NOT_FOUND")
            if co.SERVICE in original:
                raise _fail("APP_ALREADY_ROUTED_WITHOUT_STATE")
            parsed = co.parse_db_url(original)
            host, port = _source_host(req.source)
            route = co.route_line(
                parsed["dbname"], host=host, port=port, dbname=req.source.dbname,
                login=co.DbLogin(user=user, password=password),
            )  # fmt: skip
            cluster.put_secret(
                app.namespace,
                co.SECRET,
                {
                    "userlist.txt": co.userlist(
                        parsed["user"], parsed["password"], secrets.token_urlsafe(24)
                    ),
                    "route": route,
                    "original_url": original,
                    "db_alias": parsed["dbname"],
                },
                replace=False,
            )
        cluster.apply(co.manifests(app.namespace, settings.image))
        cluster.rollout_wait(app.namespace, co.NAME)
    return {"service": f"{co.SERVICE}.{app.namespace}.svc:{co.PORT}", "created": current is None}


def route_app(app: co.SourceApp, source: DbEndpoint) -> dict:
    """원본 앱의 DB 연결 문자열을 PgBouncer로 바꾸고 재시작한다(최초 1회)."""
    with OPEN_CLUSTER(source) as cluster:
        state = cluster.secret_data(app.namespace, co.SECRET)
        if not state:
            raise _fail("CUTOVER_NOT_INSTALLED")
        data = cluster.secret_data(app.namespace, app.url_secret) or {}
        routed = co.pgbouncer_url(state["original_url"], app.namespace)
        if data.get(app.url_key) == routed:
            return {"routed": False}
        cluster.put_secret(
            app.namespace, app.url_secret, data | {app.url_key: routed}, replace=True
        )
        cluster.rollout_restart(app.namespace, app.deployment)
        cluster.rollout_wait(app.namespace, app.deployment)
    return {"routed": True}


def _ensure_repl_secret(db: dm.DbPod, name: str) -> str:
    password = db.read_secret(name)
    if password is None:
        password = secrets.token_urlsafe(24)
        db.create_secret(name, password)
    return password


def switch(
    req: DataMigrationRequest, app: co.SourceApp, settings: co.CutoverSettings
) -> tuple[int, dict]:
    """`req.direction`은 지금 살아 있는 복제 방향이다. forward: 원본 → 대상 전환,
    reverse: 대상 → 원본(롤백)."""
    forward = req.direction == "forward"
    old, new = (req.source, req.target) if forward else (req.target, req.source)
    other = "reverse" if forward else "forward"
    other_secret = replication_names(req.migration_id, other).subscription.replace("_", "-")
    with dm.OPEN_DB(old) as old_db, dm.OPEN_DB(new) as new_db:
        old_login, new_login = old_db.login(), new_db.login()
        # 역방향 구독자는 옛 주 DB다. 그쪽 Secret에 비밀번호를 두고 새 주 DB에 같은 역할을 맞춘다.
        rev_password = _ensure_repl_secret(old_db, other_secret)
        new_db.execute(q.ensure_repl_role_sql(rev_password))
    src_host, tgt_host = _source_host(req.source), (req.target.peer_host, req.target.peer_port)
    old_host, new_host = (src_host, tgt_host) if forward else (tgt_host, src_host)
    with OPEN_CLUSTER(req.source) as cluster:
        state = cluster.secret_data(app.namespace, co.SECRET)
        if not state:
            raise _fail("CUTOVER_NOT_INSTALLED")
        admin_password = co_admin_password(state["userlist.txt"])
        config = co.runner_config(
            migration_id=req.migration_id, direction=req.direction, old=old, new=new,
            old_login=co.DbLogin(user=old_login[0], password=old_login[1]),
            new_login=co.DbLogin(user=new_login[0], password=new_login[1]),
            old_host=old_host, new_host=new_host, rev_password=rev_password,
            admin_password=admin_password, db_alias=state["db_alias"],
            policy=req.policy, settings=settings,
        )  # fmt: skip
        pod = cluster.pod_for(app.namespace, f"app.kubernetes.io/name={co.NAME}")
        rc, result = cluster.run_runner(app.namespace, pod, config)
        if result.get("switched"):
            # Pod를 다시 만들어도 새 주 DB로 붙도록 Secret의 연결 대상도 바꾼다.
            cluster.put_secret(
                app.namespace, co.SECRET, state | {"route": config["new_route"]}, replace=True
            )
    return rc, result


def co_admin_password(userlist_text: str) -> str:
    for line in userlist_text.splitlines():
        if line.startswith(f'"{co.ADMIN_USER}" '):
            return line.split(" ", 1)[1].strip().strip('"').replace('""', '"')
    raise _fail("CUTOVER_STATE_INVALID")


# ---- Activity ----


async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except CommandError as exc:
        raise ApplicationError("DB_COMMAND_FAILED", type="DB_COMMAND_FAILED") from exc


def _parse(payload: dict) -> tuple[DataMigrationRequest, co.SourceApp, co.CutoverSettings]:
    return (
        DataMigrationRequest.model_validate(payload["request"]),
        co.SourceApp.model_validate(payload["app"]),
        co.CutoverSettings.model_validate(payload["settings"]),
    )


@activity.defn
async def dm_install_cutover(payload: dict) -> dict:
    req, app, settings = _parse(payload)
    return await _run(install, req, app, settings)


@activity.defn
async def dm_route_app(payload: dict) -> dict:
    req, app, _ = _parse(payload)
    return await _run(route_app, app, req.source)


@activity.defn
async def dm_switchover(payload: dict) -> dict:
    """DB 전환(또는 롤백). 결과를 cutover.* 이벤트로 내고, 전환하지 않았으면 실패로 끝낸다."""
    req, app, settings = _parse(payload)
    started = time.time()
    rc, result = await _run(switch, req, app, settings)
    names = ("source", "target") if req.direction == "forward" else ("target", "source")
    if result.get("switched"):
        bridge_view = {"from": names[0], "to": names[1], **result}
        for draft in switchover_events(bridge_view):
            await emit_event_async(
                req.migration_id, draft.type, PHASE, draft.status, draft.data,
                progress=draft.progress, idempotency_key=f"{started}:{draft.key}",
            )  # fmt: skip
    summary = {
        "switched": bool(result.get("switched")),
        "active": names[1] if result.get("switched") else names[0],
        "write_pause_ms": result.get("write_pause_ms"),
        "verify_ok": result.get("verify_ok"),
        "mismatched_tables": result.get("mismatched_tables", []),
        "steps_ms": result.get("steps_ms", {}),
        "rc": rc,
    }
    if not result.get("switched"):
        code = str(result.get("code") or "SWITCHOVER_FAILED")
        log_event(req.migration_id, "error", "SWITCHOVER_REFUSED", {"code": code, "rc": rc})
        raise ApplicationError(code, summary, type=code, non_retryable=True)
    if result.get("verify_ok") is False:
        log_event(req.migration_id, "error", "SWITCHOVER_VERIFY_MISMATCH",
                  {"tables": summary["mismatched_tables"]})  # fmt: skip
    return summary
