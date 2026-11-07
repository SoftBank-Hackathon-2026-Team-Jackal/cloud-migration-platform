"""원본·대상 클러스터 사이 WireGuard 복제 경로를 만드는 Activity(DM-011, ticket 10).

`cmp_data.tunnel`이 만든 매니페스트를 두 클러스터에 적용한다. 순서: 키(각 클러스터 Secret) → 입구
게이트웨이·UDP LB → LB 주소 대기 → 반대편 게이트웨이(그 주소로 접속) → handshake 확인.
대상 DB 허용 정책은 입구 방향과 관계없이 대상 클러스터에 둔다.
같은 입력으로 다시 실행해도 된다(키는 Secret에 있으면 그대로 쓴다).

WireGuard 비밀키는 Worker 메모리에서 만들어 `kubectl create -f -`(stdin)로 Secret에만 넣는다. 결과·로그·
Workflow 이력에는 LB 주소와 handshake 여부만 남긴다.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager

from cmp_data import DataMigrationRequest, DbEndpoint
from cmp_data import tunnel as wg
from temporalio import activity
from temporalio.exceptions import ApplicationError

from cmp_worker.activities import data_migration as dm
from cmp_worker.activities.data_cutover import KubectlCluster
from cmp_worker.activities.k8s_workloads_cli import CommandError


class TunnelCluster(KubectlCluster):
    def delete(self, namespace: str, kind: str, name: str) -> None:
        # Service finalizer가 LBC 자원을 정리할 때까지 기다린다. 재시도 시 없는 자원은 성공이다.
        self._s._kubectl(
            "delete",
            kind,
            name,
            "-n",
            namespace,
            "--ignore-not-found",
            "--wait=true",
            "--timeout=300s",
            timeout=330,
        )

    def lb_address(self, namespace: str, service: str) -> str | None:
        item = self._s._get("service", service, namespace) or {}
        ingress = (item.get("status", {}).get("loadBalancer", {}).get("ingress")) or []
        return (ingress[0].get("ip") or ingress[0].get("hostname")) if ingress else None

    def latest_handshake(self, namespace: str, selector: str) -> int:
        pod = self.pod_for(namespace, selector)
        out = self._s._kubectl(
            "exec", "-n", namespace, pod, "-c", "wg", "--", "wg", "show", "wg0", "latest-handshakes"
        ).decode()  # fmt: skip
        stamps = [int(line.split()[-1]) for line in out.splitlines() if line.strip()]
        return max(stamps, default=0)

    def set_standing_peer(self, line: str, *, only_if_run: str | None = None) -> None:
        """Provisioning 83: 고정 게이트웨이의 peer 설정 파일을 바꾼다(게이트웨이가 1초 안에 반영).

        `only_if_run`이면 지금 그 실행의 설정일 때만 바꾼다(정리가 다른 실행의 peer를 지우지 않게).
        """
        pod = self.pod_for(wg.NAMESPACE, f"cmp.dev/data-tunnel={wg.STANDING_GATEWAY}")
        path = wg.STANDING_PEER_FILE
        script = f"cat > {path}.new && mv {path}.new {path}"
        if only_if_run:
            script = f'[ "$(cut -d" " -f1 {path} 2>/dev/null)" = "$1" ] || exit 0; ' + script
        self._s._kubectl(
            "exec", "-i", "-n", wg.NAMESPACE, pod, "-c", "wg", "--",
            "sh", "-c", script, "_", only_if_run or "", stdin=line.encode(),
        )  # fmt: skip

    def standing_run(self) -> str | None:
        item = self._s._get("service", wg.STANDING_ENDPOINT, wg.NAMESPACE) or {}
        return (item.get("metadata", {}).get("annotations") or {}).get(wg.RUN_ANNOTATION)


@contextmanager
def open_cluster(endpoint: DbEndpoint) -> Iterator[TunnelCluster]:
    with dm._runtime_for(endpoint) as session:
        yield TunnelCluster(session)


OPEN_CLUSTER = open_cluster  # 시험에서 가짜로 바꾼다
SLEEP = time.sleep


# Provisioning 82: establish/cleanup은 asyncio.to_thread 안에서 돈다. temporalio의 async Activity heartbeat는
# 이벤트 루프에서 task를 만들므로 스레드에서 부르면 보내지지 않고 "never awaited" 경고만 남는다. 루프로 넘긴다.
_LOOP: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = contextvars.ContextVar(
    "data_tunnel_loop", default=None
)


def _beat(detail: str) -> None:
    loop = _LOOP.get()
    try:
        if loop is None:
            activity.heartbeat(detail)
        else:
            ctx = contextvars.copy_context()  # Activity 문맥을 루프 쪽 호출에도 넘긴다
            loop.call_soon_threadsafe(ctx.run, activity.heartbeat, detail)
    except RuntimeError:  # 시험처럼 Activity 밖에서 부를 때, 루프가 이미 닫혔을 때
        pass


async def _in_thread(fn, *args):
    _LOOP.set(asyncio.get_running_loop())  # to_thread가 문맥을 복사하므로 스레드에서 보인다
    return await asyncio.to_thread(fn, *args)


def _fail(code: str, *, retryable: bool = False) -> ApplicationError:
    return ApplicationError(code, type=code, non_retryable=not retryable)


def _key(cluster: TunnelCluster, secret: str) -> str:
    """클러스터 Secret의 비밀키에서 공개키를 구한다. 없으면 만들어 넣는다. 비밀키는 반환하지 않는다."""
    # Agent 터널 모듈은 무거운 의존성을 끌고 온다(Workflow 샌드박스 검사 경로에서 import되지 않게 여기서)
    from cmp_providers.agent_tunnel import generate_private_key, public_key

    data = cluster.secret_data(wg.NAMESPACE, secret)
    private = (data or {}).get(wg.SECRET_KEY)
    if not private:
        raw = generate_private_key()
        private = base64.b64encode(raw).decode()
        cluster.put_secret(wg.NAMESPACE, secret, {wg.SECRET_KEY: private}, replace=data is not None)
    return base64.b64encode(public_key(base64.b64decode(private))).decode()


def _release(target: DbEndpoint) -> str:
    return target.pod.rsplit("-", 1)[0]  # StatefulSet Pod `<release>-0`


def establish(req: DataMigrationRequest, tunnel: wg.WireGuardTunnel) -> dict:
    base = wg.name(req.migration_id)
    lb_service = f"{base}-endpoint"
    selector = f"cmp.dev/data-tunnel={base}"
    if not req.source.local_host or not req.target.local_host:
        raise _fail("TUNNEL_DB_ADDRESS_MISSING")
    if tunnel.standing_ingress:
        return _establish_standing(req, tunnel)
    with OPEN_CLUSTER(req.target) as target, OPEN_CLUSTER(req.source) as source:
        for cluster in (target, source):
            cluster.apply([wg.namespace_manifest()])
        target_pub, source_pub = _key(target, base), _key(source, base)

        clusters = {"target": target, "source": source}
        endpoints = {"target": req.target, "source": req.source}
        public_keys = {"target": target_pub, "source": source_pub}
        ingress = tunnel.ingress
        caller: wg.Side = "target" if ingress == "source" else "source"

        def gateway(side: wg.Side, endpoint: str | None) -> list[dict]:
            other: wg.Side = "target" if side == "source" else "source"
            manifests = wg.gateway_manifests(
                req.migration_id,
                side,
                image=tunnel.image,
                local_db=endpoints[side].local_host,
                peer_public_key=public_keys[other],
                peer_endpoint=endpoint,
                ingress=ingress,
            )
            if side == ingress:
                manifests.append(
                    wg.endpoint_manifest(
                        req.migration_id,
                        tunnel.egress_cidrs,
                        ingress=ingress,
                    )
                )
            if side == "target":
                manifests.append(
                    wg.db_ingress_policy(
                        req.migration_id,
                        req.target.namespace,
                        _release(req.target),
                    )
                )
            return manifests

        clusters[ingress].apply(gateway(ingress, None))
        endpoint_deadline = time.monotonic() + tunnel.endpoint_timeout_s
        address = None
        while not (address := clusters[ingress].lb_address(wg.NAMESPACE, lb_service)):
            if time.monotonic() > endpoint_deadline:
                raise _fail("TUNNEL_ENDPOINT_TIMEOUT", retryable=True)
            _beat(f"waiting for the {ingress} UDP load balancer")
            SLEEP(5)

        clusters[caller].apply(gateway(caller, f"{address}:{wg.WG_PORT}"))
        for cluster in (target, source):
            cluster.rollout_wait(wg.NAMESPACE, base)

        deadline = time.monotonic() + tunnel.handshake_timeout_s
        if ingress == "source":
            # NLB DNS는 즉시 나와도 IP 대상의 TCP health는 약 150초 뒤 통과한다(PoC 69).
            # AWS 조회 권한을 추가하지 않고 실제 handshake로 통신 준비까지 확인한다.
            deadline = max(deadline, endpoint_deadline)
        _wait_handshake(clusters[caller], selector, deadline)
    return {"name": base, "endpoint": address, "handshake": True,
            "peer_host": wg.peer_host(req.migration_id)}  # fmt: skip


def _wait_handshake(caller: TunnelCluster, selector: str, deadline: float) -> None:
    while True:
        try:
            if caller.latest_handshake(wg.NAMESPACE, selector) > 0:
                return
        except CommandError:
            pass  # 게이트웨이가 아직 wg0를 만들지 않았다
        if time.monotonic() > deadline:
            raise _fail("TUNNEL_HANDSHAKE_TIMEOUT", retryable=True)
        _beat("waiting for the WireGuard handshake")
        SLEEP(1)


# 고정 게이트웨이를 다른 실행이 쓰고 있다고 볼 handshake 나이. WireGuard는 트래픽(keepalive 25초 포함)이 있으면
# 2분마다 다시 handshake하므로, 이보다 오래됐으면 그 터널은 끝났거나 버려진 것이다.
STANDING_BUSY_HANDSHAKE_S = 300


def _claim_standing(source: TunnelCluster, base: str) -> None:
    """고정 게이트웨이는 한 번에 한 실행만 쓴다. 다른 실행이 지금 쓰고 있으면 `TUNNEL_IN_USE`로 멈춘다.

    다른 실행의 peer를 덮어쓰면 그 실행의 복제와, 전환 뒤 PgBouncer로 그 터널을 쓰는 원본 앱이 끊긴다(2026-11-07
    11:39 원본 chat-app이 그렇게 멈췄다). 쓰는 실행 이름은 고정 NLB의 주석에 있다. 주석이 다른 실행이어도 최근
    handshake가 없으면(정리하지 못하고 끝난 실행) 지금처럼 가져간다. 게이트웨이가 아직 없으면 쓰는 실행도 없다.
    """
    owner = source.standing_run()
    if owner in (None, wg.STANDING_IDLE, base):
        return
    try:
        last = source.latest_handshake(wg.NAMESPACE, f"cmp.dev/data-tunnel={wg.STANDING_GATEWAY}")
    except CommandError:
        return
    if last and time.time() - last < STANDING_BUSY_HANDSHAKE_S:
        raise _fail("TUNNEL_IN_USE")


def _establish_standing(req: DataMigrationRequest, tunnel: wg.WireGuardTunnel) -> dict:
    """Provisioning 83: 원본 고정 NLB + 고정 게이트웨이. 원본은 peer 설정만 바꾸고, 대상 게이트웨이만 새로 띄운다.

    고정 게이트웨이가 없으면(미리 만들지 않음) 여기서 만든다. 그 실행은 NLB 대상 등록을 기다리므로 느리다.
    순서: 원본 고정 게이트웨이·NLB·peer-db Service → 원본 peer 설정 → 대상 게이트웨이(접속하는 쪽) → handshake.
    원본 peer를 먼저 바꿔 두어 대상의 첫 handshake가 거절되지 않게 한다.
    """
    base = wg.name(req.migration_id)
    selector = f"cmp.dev/data-tunnel={base}"
    with OPEN_CLUSTER(req.target) as target, OPEN_CLUSTER(req.source) as source:
        _claim_standing(source, base)
        for cluster in (target, source):
            cluster.apply([wg.namespace_manifest()])
        target_pub, source_pub = _key(target, base), _key(source, wg.STANDING_GATEWAY)

        source.apply([
            *wg.standing_gateway_manifests(tunnel.image),
            wg.endpoint_manifest(req.migration_id, tunnel.egress_cidrs, ingress="source",
                                 standing=True),
            wg.peer_db_service(req.migration_id, standing=True),
        ])  # fmt: skip
        endpoint_deadline = time.monotonic() + tunnel.endpoint_timeout_s
        while not (address := source.lb_address(wg.NAMESPACE, wg.STANDING_ENDPOINT)):
            if time.monotonic() > endpoint_deadline:
                raise _fail("TUNNEL_ENDPOINT_TIMEOUT", retryable=True)
            _beat("waiting for the source UDP load balancer")
            SLEEP(5)
        source.rollout_wait(wg.NAMESPACE, wg.STANDING_GATEWAY)
        source.set_standing_peer(
            wg.standing_peer_line(req.migration_id, target_pub, req.source.local_host)
        )

        target.apply([
            *wg.gateway_manifests(req.migration_id, "target", image=tunnel.image,
                                  local_db=req.target.local_host, peer_public_key=source_pub,
                                  peer_endpoint=f"{address}:{wg.WG_PORT}", ingress="source"),
            wg.db_ingress_policy(req.migration_id, req.target.namespace, _release(req.target)),
        ])  # fmt: skip
        target.rollout_wait(wg.NAMESPACE, base)
        # 처음 만든 고정 게이트웨이는 NLB 대상 등록·healthy까지 기다려야 하므로 주소 대기 예산까지 둔다.
        deadline = max(time.monotonic() + tunnel.handshake_timeout_s, endpoint_deadline)
        _wait_handshake(target, selector, deadline)
    return {"name": base, "endpoint": address, "handshake": True,
            "peer_host": wg.peer_host(req.migration_id)}  # fmt: skip


def cleanup(req: DataMigrationRequest, tunnel: wg.WireGuardTunnel) -> dict:
    """복제·롤백·PgBouncer가 터널을 더 이상 쓰지 않을 때 명시적으로 호출한다.

    입구 Service 삭제가 실패하면 나머지 리소스를 보존한다. 공유 Namespace는 삭제하지 않는다.
    고정 입구(`standing_ingress`, Provisioning 82·83)는 `_cleanup_standing`이 맡는다.
    """
    base = wg.name(req.migration_id)
    if tunnel.standing_ingress:
        return _cleanup_standing(req, tunnel)
    with OPEN_CLUSTER(req.source) as source, OPEN_CLUSTER(req.target) as target:
        ingress = source if tunnel.ingress == "source" else target
        _beat("deleting the UDP load balancer Service")
        ingress.delete(wg.NAMESPACE, "service", f"{base}-endpoint")
        for cluster in (source, target):
            for kind, resource in (
                ("deployment", base),
                ("service", f"{base}-peer-db"),
                ("configmap", base),
                ("secret", base),
            ):
                _beat("deleting data tunnel resources")
                cluster.delete(wg.NAMESPACE, kind, resource)
        target.delete(req.target.namespace, "networkpolicy", f"{base}-db-in")
    return {"name": base, "cleaned": True}


def _cleanup_standing(req: DataMigrationRequest, tunnel: wg.WireGuardTunnel) -> dict:
    """Provisioning 83: 대상 게이트웨이·키·정책과 원본 peer-db Service를 지우고, 원본 고정 게이트웨이의 peer를 비운다.

    고정 NLB·고정 게이트웨이·그 키는 남긴다. 다른 실행이 이미 가져갔으면 그 실행의 설정은 건드리지 않는다.
    """
    base = wg.name(req.migration_id)
    with OPEN_CLUSTER(req.source) as source, OPEN_CLUSTER(req.target) as target:
        _beat("clearing the standing gateway peer")
        try:
            source.set_standing_peer("", only_if_run=base)
        except CommandError:
            pass  # 고정 게이트웨이가 없다(이미 지움). 남은 자원 정리는 계속한다.
        if source.standing_run() == base:
            idle = wg.standing_endpoint_manifest(tunnel.egress_cidrs)
            source.apply([idle])
        _beat("deleting data tunnel resources")
        source.delete(wg.NAMESPACE, "service", f"{base}-peer-db")
        for kind, resource in (
            ("deployment", base),
            ("service", f"{base}-peer-db"),
            ("configmap", base),
            ("secret", base),
        ):
            _beat("deleting data tunnel resources")
            target.delete(wg.NAMESPACE, kind, resource)
        target.delete(req.target.namespace, "networkpolicy", f"{base}-db-in")
    return {"name": base, "cleaned": True}


@activity.defn
async def dm_tunnel_cleanup(payload: dict) -> dict:
    """payload는 dm_tunnel과 같다. Workflow 종료 시 자동으로 부르지 않는다."""
    req = DataMigrationRequest.model_validate(payload["request"])
    tunnel = wg.WireGuardTunnel.model_validate(payload["tunnel"])
    try:
        return await _in_thread(cleanup, req, tunnel)
    except CommandError as exc:
        raise _fail("TUNNEL_CLEANUP_FAILED", retryable=True) from exc


@activity.defn
async def dm_tunnel(payload: dict) -> dict:
    """payload: {request: DataMigrationRequest, tunnel: WireGuardTunnel}"""
    req = DataMigrationRequest.model_validate(payload["request"])
    tunnel = wg.WireGuardTunnel.model_validate(payload["tunnel"])
    # Provisioning 77: the web sees the (minutes-long) tunnel step, like dm_precheck's "preflight".
    await dm._phase(req, "tunnel")
    try:
        return await _in_thread(establish, req, tunnel)
    except CommandError as exc:
        raise ApplicationError("TUNNEL_COMMAND_FAILED", type="TUNNEL_COMMAND_FAILED") from exc


__all__ = ["cleanup", "dm_tunnel", "dm_tunnel_cleanup", "establish"]
