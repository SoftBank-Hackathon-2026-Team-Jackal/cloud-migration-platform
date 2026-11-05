"""원본 클러스터의 `cmp-cutover` Pod(PgBouncer + 전환 실행기) 정의와 설정 조립(DM-010, ticket 06·04).

비밀값(앱 비밀번호, DB 관리자 비밀번호, 복제 역할 비밀번호)은 여기서 만드는 매니페스트에 넣지 않는다.
Secret 내용과 실행기 설정은 Activity가 메모리에서 조립해 stdin으로만 넘긴다.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote, urlsplit

from pydantic import Field

from .contracts import DbEndpoint, Direction, ReadinessPolicy, Strict
from .logic import replication_names
from .sql import MARKER_TABLE, REPL_ROLE, TOOL_APP_NAME, conninfo

NAME = "cmp-cutover"
SERVICE = "cmp-pgbouncer"
SECRET = "cmp-cutover"
PORT = 6432
ADMIN_USER = "cmp_admin"
ROUTE_FILE = "/etc/pgbouncer/runtime/databases.ini"
LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/managed-by": "cmp-data-migration"}


class SourceApp(Strict):
    """원본 앱. DB 연결 문자열이 든 Secret을 PgBouncer 주소로 바꾸고 Deployment를 재시작한다."""

    namespace: str
    deployment: str
    url_secret: str
    url_key: str = "DATABASE_URL"


class CutoverSettings(Strict):
    image: str
    pause_timeout_sec: float = Field(default=5, gt=0)
    marker_timeout_ms: int = Field(default=10_000, gt=0)
    wait_ready_sec: float = Field(default=30, ge=0)
    seq_headroom: int = Field(default=10_000, ge=0)
    verify: str = Field(default="auto", pattern="^(auto|checksum|light|none)$")


class DbLogin(Strict):
    user: str
    password: str


def parse_db_url(url: str) -> dict[str, Any]:
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql") or not parts.hostname:
        raise ValueError("INVALID_DATABASE_URL")
    return {
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "host": parts.hostname,
        "port": parts.port or 5432,
        "dbname": unquote(parts.path.lstrip("/")),
        "query": parts.query,
    }


def pgbouncer_url(original: str, namespace: str) -> str:
    """앱 연결 문자열의 호스트·포트만 PgBouncer로 바꾼다(사용자·비밀번호·DB 이름·옵션 유지)."""
    p = parse_db_url(original)
    user, password = quote(p["user"], safe=""), quote(p["password"], safe="")
    query = f"?{p['query']}" if p["query"] else ""
    host = f"{SERVICE}.{namespace}.svc"
    return f"postgresql://{user}:{password}@{host}:{PORT}/{quote(p['dbname'], safe='')}{query}"


def route_line(db_alias: str, *, host: str, port: int, dbname: str, login: DbLogin) -> str:
    """PgBouncer `[databases]` 한 줄. 서버 쪽 로그인은 이 값으로 하고, 앱은 자기 사용자로 붙는다."""
    info = conninfo(host=host, port=port, dbname=dbname, user=login.user, password=login.password)
    return f"{db_alias} = {info}"


def userlist(app_user: str, app_password: str, admin_password: str) -> str:
    def esc(value: str) -> str:
        return value.replace('"', '""')

    return f'"{esc(app_user)}" "{esc(app_password)}"\n"{ADMIN_USER}" "{esc(admin_password)}"\n'


def manifests(namespace: str, image: str) -> list[dict[str, Any]]:
    """Deployment(pgbouncer + runner)와 Service. Secret은 Activity가 따로 만든다."""
    secret_volume = {"name": "secret", "secret": {"secretName": SECRET}}
    runtime_volume = {"name": "runtime", "emptyDir": {}}
    mounts = [
        {"name": "secret", "mountPath": "/etc/pgbouncer/secret", "readOnly": True},
        {"name": "runtime", "mountPath": "/etc/pgbouncer/runtime"},
    ]
    security = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": namespace, "labels": LABELS},
        "spec": {
            "replicas": 1,
            # 한 대만 둔다. 두 대면 PAUSE·연결 대상 변경이 한쪽에만 적용된다.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
            "template": {
                "metadata": {"labels": LABELS},
                "spec": {
                    "volumes": [secret_volume, runtime_volume],
                    "containers": [
                        {
                            "name": "pgbouncer",
                            "image": image,
                            "command": ["cmp-pgbouncer"],
                            "ports": [{"name": "pgbouncer", "containerPort": PORT}],
                            "volumeMounts": mounts,
                            "securityContext": security,
                            "readinessProbe": {
                                "tcpSocket": {"port": PORT},
                                "periodSeconds": 2,
                            },
                            "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}},
                        },
                        {
                            "name": "runner",
                            "image": image,
                            "command": ["sleep", "infinity"],
                            "volumeMounts": mounts,
                            "securityContext": security,
                            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
                        },
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": SERVICE, "namespace": namespace, "labels": LABELS},
        "spec": {
            "selector": {"app.kubernetes.io/name": NAME},
            "ports": [{"name": "pgbouncer", "port": PORT, "targetPort": PORT}],
        },
    }
    return [deployment, service]


def admin_url(endpoint_host: str, port: int, dbname: str, login: DbLogin) -> str:
    user, password = quote(login.user, safe=""), quote(login.password, safe="")
    return f"postgresql://{user}:{password}@{endpoint_host}:{port}/{quote(dbname, safe='')}"


def runner_config(
    *,
    migration_id: str,
    direction: Direction,
    old: DbEndpoint,
    new: DbEndpoint,
    old_login: DbLogin,
    new_login: DbLogin,
    old_host: tuple[str, int],
    new_host: tuple[str, int],
    rev_password: str,
    admin_password: str,
    db_alias: str,
    policy: ReadinessPolicy,
    settings: CutoverSettings,
) -> dict[str, Any]:
    """전환 실행기 설정. `direction`은 지금 살아 있는 복제 방향이다(forward면 원본 → 대상 전환).

    old_host·new_host는 cmp-cutover Pod(원본 클러스터)에서 각 DB로 가는 주소다.
    역방향 구독은 옛 주 DB에서 새 주 DB의 `peer_host`로 접속한다.
    """
    live = replication_names(migration_id, direction)
    other = replication_names(migration_id, "reverse" if direction == "forward" else "forward")
    return {
        "old_url": admin_url(*old_host, old.dbname, old_login),
        "new_url": admin_url(*new_host, new.dbname, new_login),
        "pgb_admin_url": admin_url("127.0.0.1", PORT, "pgbouncer", DbLogin(user=ADMIN_USER, password=admin_password)),
        "pgb_db": db_alias,
        "route_file": ROUTE_FILE,
        "new_route": route_line(
            db_alias, host=new_host[0], port=new_host[1], dbname=new.dbname, login=new_login
        ),
        "fwd_sub": live.subscription,
        "rev_pub": other.publication,
        "rev_sub": other.subscription,
        "rev_conninfo": conninfo(
            host=new.peer_host, port=new.peer_port, dbname=new.dbname, user=REPL_ROLE,
            password=rev_password,
        ),
        "marker_table": MARKER_TABLE,
        "max_xact_ms": policy.max_xact_ms,
        "lag_ok_bytes": policy.lag_ok_bytes,
        "wait_ready_sec": settings.wait_ready_sec,
        "pause_timeout_sec": settings.pause_timeout_sec,
        "marker_timeout_ms": settings.marker_timeout_ms,
        "seq_headroom": settings.seq_headroom,
        "verify": settings.verify,
        "app_name": TOOL_APP_NAME,
    }  # fmt: skip
