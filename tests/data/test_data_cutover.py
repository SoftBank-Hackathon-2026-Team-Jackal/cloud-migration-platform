"""DB 전환(ticket 04·06) 시험: 설정 조립(순수)과 Activity(가짜 클러스터·DB).

실제 PgBouncer·전환 실행기 동작은 data-lab(k3d)에서 확인한다(infra/local/data-lab/README.md).
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from cmp_api.migration_events import InMemoryEmitter
from cmp_contracts.events import validate_data
from cmp_data import DataMigrationRequest, ReadinessPolicy, replication_names
from cmp_data import cutover as co
from cmp_worker.activities import data_cutover as dc
from cmp_worker.activities import data_migration as dm
from cmp_worker.activities.k8s_workloads_cli import CommandError
from cmp_worker.events import configure_emitter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

MID = "migration-3f2b9c1e-5d4a-4c8b-9e7f-0a1b2c3d4e5f"


def ep(ref: str, host: str, local: str | None = None) -> dict:
    out = {"access": "KUBECONFIG", "kubeconfig_ref": ref, "namespace": "db",
           "pod": "db-0", "peer_host": host, "dbname": "chat"}  # fmt: skip
    return out | ({"local_host": local} if local else {})


REQUEST = {
    "migration_id": MID,
    "source": ep("src", "10.9.0.1", "db.db.svc"),
    "target": ep("dst", "10.9.0.2"),
}
APP = {"namespace": "chat", "deployment": "chat-app", "url_secret": "chat-secrets"}
SETTINGS = {"image": "registry/cmp-data-cutover:1"}
PAYLOAD = {"request": REQUEST, "app": APP, "settings": SETTINGS}
APP_URL = "postgresql://chat:s3cr%40t@db.chat.svc:5432/chat?sslmode=disable"


# ---- 순수 ----


def test_pgbouncer_url_keeps_credentials_and_options_but_moves_host() -> None:
    assert co.pgbouncer_url(APP_URL, "chat") == (
        "postgresql://chat:s3cr%40t@cmp-pgbouncer.chat.svc:6432/chat?sslmode=disable"
    )


def test_manifests_hold_no_secret_values_and_run_one_replica() -> None:
    deployment, service = co.manifests("chat", "img:1")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert service["spec"]["ports"][0]["port"] == 6432
    assert "password" not in json.dumps([deployment, service]).lower()


def test_runner_config_for_forward_switch_wires_reverse_replication() -> None:
    req = DataMigrationRequest.model_validate(REQUEST)
    login = co.DbLogin(user="postgres", password="pw")
    cfg = co.runner_config(
        migration_id=MID, direction="forward", old=req.source, new=req.target,
        old_login=login, new_login=login, old_host=("db.db.svc", 5432),
        new_host=("10.9.0.2", 5432), rev_password="rev-pw", admin_password="adm",
        db_alias="chat", policy=ReadinessPolicy(), settings=co.CutoverSettings(image="x"),
    )  # fmt: skip
    fwd, rev = replication_names(MID, "forward"), replication_names(MID, "reverse")
    assert cfg["fwd_sub"] == fwd.subscription
    assert (cfg["rev_sub"], cfg["rev_pub"]) == (rev.subscription, rev.publication)
    # 역방향 구독(옛 주 = 원본)은 새 주(대상)의 터널 주소로 접속한다
    assert "host='10.9.0.2'" in cfg["rev_conninfo"] and "user='cmp_repl'" in cfg["rev_conninfo"]
    assert cfg["new_route"].startswith("chat = host='10.9.0.2'")


# ---- Activity (가짜) ----


class FakeCluster:
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], dict[str, str]] = {
            ("chat", "chat-secrets"): {"DATABASE_URL": APP_URL}
        }
        self.applied: list[dict] = []
        self.restarts: list[str] = []
        self.runner_result: tuple[int, dict] = (0, {})
        self.runner_configs: list[dict] = []
        self.service_reachable = True

    def secret_data(self, namespace, name):
        return self.secrets.get((namespace, name))

    def put_secret(self, namespace, name, data, *, replace):
        assert replace == ((namespace, name) in self.secrets)
        self.secrets[(namespace, name)] = dict(data)

    def apply(self, manifests):
        self.applied.extend(manifests)

    def rollout_restart(self, namespace, deployment):
        self.restarts.append(deployment)

    def rollout_wait(self, namespace, deployment, timeout=300):
        pass

    def wait_old_pods_gone(self, namespace, deployment, timeout=300):
        self.restarts.append(f"wait:{deployment}")

    def pod_for(self, namespace, selector):
        return "cmp-cutover-abc"

    def wait_service(self, namespace, pod, host, port, timeout=60):
        self.restarts.append(f"reach:{host}:{port}")
        return self.service_reachable

    def run_runner(self, namespace, pod, config):
        self.runner_configs.append(config)
        if isinstance(self.runner_result, Exception):
            raise self.runner_result
        return self.runner_result


class FakeDb:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.executed: list[str] = []

    def login(self):
        return ("postgres", "admin-pw")

    def read_secret(self, name):
        return self.secrets.get(name)

    def create_secret(self, name, value):
        self.secrets[name] = value

    def execute(self, sql):
        self.executed.append(sql)


@pytest.fixture
def fakes(monkeypatch):
    cluster, dbs = FakeCluster(), {"src": FakeDb(), "dst": FakeDb()}

    @contextmanager
    def open_cluster(_):
        yield cluster

    @contextmanager
    def open_db(endpoint):
        yield dbs[endpoint.kubeconfig_ref]

    monkeypatch.setattr(dc, "OPEN_CLUSTER", open_cluster)
    monkeypatch.setattr(dm, "OPEN_DB", open_db)
    return cluster, dbs


@pytest.fixture
def emitter():
    emitter = InMemoryEmitter()
    previous = configure_emitter(emitter)
    yield emitter
    configure_emitter(previous)


async def run(activity, payload):
    return await ActivityEnvironment().run(activity, payload)


async def test_install_then_route_app_once_and_keep_the_original_url(fakes) -> None:
    cluster, _ = fakes
    assert (await run(dc.dm_install_cutover, PAYLOAD))["created"]
    assert not (await run(dc.dm_install_cutover, PAYLOAD))["created"]
    state = cluster.secrets[("chat", co.SECRET)]
    assert state["original_url"] == APP_URL and state["db_alias"] == "chat"
    assert state["route"].startswith("chat = host='db.db.svc'")

    assert (await run(dc.dm_route_app, PAYLOAD))["routed"]
    assert not (await run(dc.dm_route_app, PAYLOAD))["routed"]
    # 재시작 전에 Service 주소로 PgBouncer에 접속되는지 확인하고, 재시작 뒤 옛 Pod(원본 DB에 직접 붙은)가
    # 사라질 때까지 기다린다. 재실행도 다시 확인한다.
    assert cluster.restarts == [
        "reach:cmp-pgbouncer.chat.svc:6432",
        "chat-app",
        "wait:chat-app",
        "wait:chat-app",
    ]
    assert (
        "cmp-pgbouncer.chat.svc:6432" in cluster.secrets[("chat", "chat-secrets")]["DATABASE_URL"]
    )


async def test_route_app_waits_until_the_pgbouncer_service_answers(fakes) -> None:
    """PgBouncer Pod가 Ready여도 Service 주소(DNS·kube-proxy 규칙)는 잠시 뒤에 열린다. 그 사이 앱을
    재시작하면 새 Pod의 첫 접속이 실패한다(로컬 k3d e2e에서 2건). 열리지 않으면 앱을 건드리지 않는다."""
    cluster, _ = fakes
    await run(dc.dm_install_cutover, PAYLOAD)
    cluster.service_reachable = False
    with pytest.raises(ApplicationError) as err:
        await run(dc.dm_route_app, PAYLOAD)
    assert err.value.type == "CUTOVER_SERVICE_UNREACHABLE" and not err.value.non_retryable
    assert cluster.restarts == ["reach:cmp-pgbouncer.chat.svc:6432"]
    assert cluster.secrets[("chat", "chat-secrets")]["DATABASE_URL"] == APP_URL


async def test_switchover_emits_web_cutover_events_and_moves_the_saved_route(
    fakes, emitter
) -> None:
    cluster, dbs = fakes
    await run(dc.dm_install_cutover, PAYLOAD)
    steps = [{"id": "seq_raise", "ms": 1.2, "window": "pre"},
             {"id": "pause", "ms": 0.6, "start_ms": 0, "window": "pause"},
             {"id": "resume", "ms": 0.3, "start_ms": 6.7, "window": "pause"}]  # fmt: skip
    tables = [{"table": "messages", "source_rows": 10, "target_rows": 10, "match": True}]
    ready = {"longest_xact_ms": 3, "lock_waiters": 0, "lag_bytes": 0, "ready": True,
             "reasons": [], "waited_ms": 12.5}  # fmt: skip
    cluster.runner_result = (
        0,
        {"switched": True, "write_pause_ms": 7, "verify_ok": True, "steps_ms": {},
         "steps": steps, "tables": tables, "ready": ready},
    )  # fmt: skip
    result = await run(
        dc.dm_switchover, PAYLOAD | {"attempt": 2, "readiness": {"commits_per_sec": 50}}
    )
    assert result["switched"] and result["active"] == "target" and result["write_pause_ms"] == 7
    assert cluster.secrets[("chat", co.SECRET)]["route"].startswith("chat = host='10.9.0.2'")
    # 역방향 구독자(원본) 쪽 Secret에 비밀번호를 두고, 새 주(대상)에 같은 역할을 맞춘다
    assert list(dbs["src"].secrets) == [
        replication_names(MID, "reverse").subscription.replace("_", "-")
    ]
    assert "cmp_repl" in dbs["dst"].executed[0]
    assert all(pw not in json.dumps(result) for pw in dbs["src"].secrets.values())
    types = [e.type for e in emitter.events]
    assert types == ["cutover.ready", *["cutover.step"] * 3, "cutover.outcome", "cutover.step",
                     "cutover.verify", *["data.verify"] * 4]  # fmt: skip
    by_type = {e.type: e for e in emitter.events}
    assert by_type["cutover.ready"].data["attempt"] == 2
    assert by_type["cutover.ready"].data["commits_per_sec"] == 50
    outcome = by_type["cutover.outcome"].data
    assert outcome["result"] == "switched" and outcome["active"] == "target"
    assert outcome["pause_ms"] == 7 and outcome["replication"]["direction"] == "reverse"
    assert by_type["cutover.verify"].data["checksum"] == "match"
    assert result["steps"] == steps
    for event in emitter.events:
        validate_data(event.type, event.data)


async def test_refused_switchover_fails_without_moving_the_route(fakes, emitter) -> None:
    cluster, _ = fakes
    await run(dc.dm_install_cutover, PAYLOAD)
    before = dict(cluster.secrets[("chat", co.SECRET)])
    steps = [{"id": "pause", "ms": 0.6, "start_ms": 0, "window": "pause"},
             {"id": "marker", "ms": 10000, "start_ms": 0.6, "window": "pause"}]  # fmt: skip
    cluster.runner_result = (
        4,
        {"switched": False, "code": "MARKER_TIMEOUT", "detail": "", "paused": True,
         "steps": steps, "ready": None},
    )  # fmt: skip
    with pytest.raises(ApplicationError) as err:
        await run(dc.dm_switchover, PAYLOAD)
    assert err.value.type == "MARKER_TIMEOUT" and err.value.non_retryable
    assert cluster.secrets[("chat", co.SECRET)] == before
    # 거부도 웹에 알린다: 실패한 단계, 원본으로 재개(revert), 거부 결과. 검증은 하지 않는다.
    events = [(e.type, e.data) for e in emitter.events]
    marks = [d for t, d in events if t == "cutover.step"]
    assert [(m["id"], m["result"]) for m in marks] == [
        ("pause", "ok"),
        ("marker", "fail"),
        ("resume_source", "revert"),
    ]
    outcome = next(d for t, d in events if t == "cutover.outcome")
    assert outcome["result"] == "refused" and outcome["reason"] == "MARKER_TIMEOUT"
    assert outcome["active"] == "source" and outcome["pause_ms"] is None
    assert not [t for t, _ in events if t in ("cutover.verify", "data.verify")]
    for event in emitter.events:
        validate_data(event.type, event.data)


async def test_announce_publishes_plan_and_cutover_approval(fakes, emitter) -> None:
    sample = {"phase": "streaming", "lag_bytes": 0}
    payload = PAYLOAD | {"sample": sample, "readiness": {"commits_per_sec": 40}, "approval": True}
    result = await run(dc.dm_announce_cutover, payload)
    assert result == {"ready": True}
    plan, approval = emitter.events
    assert plan.type == "cutover.plan" and plan.status == "waiting"
    assert {p["id"]: p["state"] for p in plan.data["preconditions"]} == {
        "lag": "pass",
        "pooler": "pass",
        "reverse_sub": "pass",
    }
    assert plan.data["topology"]["writes_per_sec"] == 40
    assert approval.type == "approval.required" and approval.data["gate"] == "cutover"
    for event in emitter.events:
        validate_data(event.type, event.data)
    lagging = PAYLOAD | {"sample": {"phase": "catchup", "lag_bytes": 9 << 20}, "approval": False}
    assert await run(dc.dm_announce_cutover, lagging) == {"ready": False}
    assert emitter.events[-1].type == "cutover.plan"


async def test_announce_shows_a_shadow_warning_without_blocking_readiness(fakes, emitter) -> None:
    """Provisioning 84: 그림자 리허설 줄은 표시만 한다(warn이어도 준비됨)."""
    line = {"id": "shadow", "state": "warn", "value": "1238/1240",
            "evidence": "match >= 0.99 · target 5xx 0"}  # fmt: skip
    payload = PAYLOAD | {"sample": {"phase": "streaming", "lag_bytes": 0}, "readiness": {},
                         "approval": False, "shadow": line}  # fmt: skip
    assert await run(dc.dm_announce_cutover, payload) == {"ready": True}
    plan = emitter.events[-1]
    assert plan.data["preconditions"][-1] == line
    validate_data(plan.type, plan.data)


async def test_unreachable_runner_is_reported_as_refused(fakes, emitter) -> None:
    cluster, _ = fakes
    await run(dc.dm_install_cutover, PAYLOAD)
    cluster.runner_result = CommandError("kubectl")
    with pytest.raises(ApplicationError) as err:
        await run(dc.dm_switchover, PAYLOAD)
    assert err.value.type == "DB_COMMAND_FAILED"
    outcome = next(e for e in emitter.events if e.type == "cutover.outcome")
    assert outcome.data["result"] == "refused" and outcome.data["active"] == "source"
    assert outcome.data["reason"] == "DB_COMMAND_FAILED"


async def test_rollback_runs_the_same_runner_in_the_reverse_direction(fakes) -> None:
    cluster, _ = fakes
    await run(dc.dm_install_cutover, PAYLOAD)
    cluster.runner_result = (
        0,
        {"switched": True, "write_pause_ms": 6, "verify_ok": True, "steps_ms": {}},
    )
    payload = PAYLOAD | {"request": REQUEST | {"direction": "reverse"}}
    result = await run(dc.dm_switchover, payload)
    assert result["active"] == "source"
    cfg = cluster.runner_configs[-1]
    assert cfg["fwd_sub"] == replication_names(MID, "reverse").subscription
    assert cfg["rev_sub"] == replication_names(MID, "forward").subscription
    assert cfg["new_route"].startswith("chat = host='db.db.svc'")
