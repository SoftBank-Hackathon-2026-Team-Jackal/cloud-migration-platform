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

    def pod_for(self, namespace, selector):
        return "cmp-cutover-abc"

    def run_runner(self, namespace, pod, config):
        self.runner_configs.append(config)
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
    assert cluster.restarts == ["chat-app"]
    assert (
        "cmp-pgbouncer.chat.svc:6432" in cluster.secrets[("chat", "chat-secrets")]["DATABASE_URL"]
    )


async def test_switchover_emits_web_cutover_events_and_moves_the_saved_route(
    fakes, emitter
) -> None:
    cluster, dbs = fakes
    await run(dc.dm_install_cutover, PAYLOAD)
    steps = {"pause_wait_inflight": 1, "marker_wait": 2, "snapshot_seq_check": 3,
             "switch_subscriptions": 1, "retarget_resume": 0}  # fmt: skip
    cluster.runner_result = (
        0,
        {"switched": True, "write_pause_ms": 7, "verify_ok": True, "steps_ms": steps},
    )
    result = await run(dc.dm_switchover, PAYLOAD)
    assert result["switched"] and result["active"] == "target" and result["write_pause_ms"] == 7
    assert cluster.secrets[("chat", co.SECRET)]["route"].startswith("chat = host='10.9.0.2'")
    # 역방향 구독자(원본) 쪽 Secret에 비밀번호를 두고, 새 주(대상)에 같은 역할을 맞춘다
    assert list(dbs["src"].secrets) == [
        replication_names(MID, "reverse").subscription.replace("_", "-")
    ]
    assert "cmp_repl" in dbs["dst"].executed[0]
    assert all(pw not in json.dumps(result) for pw in dbs["src"].secrets.values())
    types = [e.type for e in emitter.events]
    assert "cutover.step" in types and "cutover.result" in types
    for event in emitter.events:
        validate_data(event.type, event.data)


async def test_refused_switchover_fails_without_moving_the_route(fakes, emitter) -> None:
    cluster, _ = fakes
    await run(dc.dm_install_cutover, PAYLOAD)
    before = dict(cluster.secrets[("chat", co.SECRET)])
    cluster.runner_result = (
        4,
        {"switched": False, "code": "NOT_READY", "detail": "LONG_TRANSACTION"},
    )
    with pytest.raises(ApplicationError) as err:
        await run(dc.dm_switchover, PAYLOAD)
    assert err.value.type == "NOT_READY" and err.value.non_retryable
    assert cluster.secrets[("chat", co.SECRET)] == before
    assert not [e for e in emitter.events if e.type.startswith("cutover.")]


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
