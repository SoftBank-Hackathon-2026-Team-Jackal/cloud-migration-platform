"""그림자 리허설 수집기(Provisioning 83, #362). 중계 서버에서 표준 라이브러리만으로 돈다.

중계 Envoy가 허용 경로의 GET만 복사해 127.0.0.1:18090으로 보낸다(응답은 버려진다). 수집기는 같은 요청을
원본에 2번, 대상에 1번 다시 보내 비교한다.
- 원본 2번 사이에 달라진 JSON pointer는 잡음으로 배우고 비교에서 뺀다(나중에 배운 잡음도 앞선 건에 적용).
- 상태 코드·정규화 JSON(키 정렬)·지연을 비교한다. JSON이 아니면 본문 해시만 비교한다.
- 원본 재요청은 초당 `rate_per_sec`번까지다(비교 1건 = 원본 2번). 넘치면 건너뛴다(skipped).
- 본문은 보관하지 않는다(해시만). 불일치 표본은 값을 형식·길이로 가리고 쿼리 문자열을 빼고 20개만 남긴다.

하지 않는 것: 쓰기(GET 외) 재요청, 차단·수정, 설정 변경. 판정(통과 비율)은 Workflow가 한다.

제어 API(127.0.0.1:18091): POST /runs, GET /runs/{run_id}/summary, DELETE /runs/{run_id}(멱등).
실행: `python3.11 -m cmp_traffic.shadow` (환경변수는 `main` 참고).
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import math
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOG = logging.getLogger("cmp_traffic.shadow")
MIRROR_PORT = 18090
CONTROL_PORT = 18091
MAX_SAMPLES = 20
MAX_CANDIDATES = 200  # 나중에 잡음으로 밝혀질 표본을 대비해 조금 더 들고 있는다
MAX_LATENCIES = 10_000
MAX_POINTERS = 200  # 한 응답에서 기억하는 다른 pointer 수
MAX_RUNS = 16
MAX_RUN_SECONDS = 900  # 끄기를 못 받아도 이 시간이 지나면 스스로 멈춘다
UPSTREAM_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 2 * 1024 * 1024

# 다시 보낼 때 빼는 헤더: 연결 단위 헤더, Envoy 내부 헤더, 원래 요청 경로 정보.
_DROP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "host",
    "content-length",
    "x-request-id",
}
_DROP_PREFIXES = ("x-envoy-", "x-forwarded-")

Address = tuple[str, int]


@dataclass(frozen=True)
class Reply:
    status: int | None  # None = 연결 실패·시간 초과
    body: bytes
    ms: float


Fetch = Callable[[Address, str, dict[str, str]], Reply]


def strip_shadow_host(host: str) -> str:
    """Envoy가 복사본 Host에 붙이는 `-shadow`를 뗀다(포트가 있으면 그 앞에서)."""
    name, sep, port = host.rpartition(":") if ":" in host else (host, "", "")
    name = name.removesuffix("-shadow")
    return f"{name}{sep}{port}"


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """JSON 값을 {JSON pointer: 잎 값}으로. 키는 정렬하고 빈 객체·배열은 그 자체가 잎이다."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key in sorted(value):
            out.update(flatten(value[key], f"{prefix}/{_escape(str(key))}"))
        return out
    if isinstance(value, list) and value:
        out = {}
        for index, item in enumerate(value):
            out.update(flatten(item, f"{prefix}/{index}"))
        return out
    return {prefix: value}


_MISSING = object()


def mask(value: Any) -> str:
    """값을 드러내지 않고 형식·크기만 남긴다. null만 그대로 보인다."""
    if value is _MISSING:
        return "<missing>"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int | float):
        return "<number>"
    if isinstance(value, str):
        return f"<string:{len(value)}>"
    if isinstance(value, list):
        return f"<array:{len(value)}>"
    if isinstance(value, dict):
        return f"<object:{len(value)}>"
    return "<value>"


def _leaves(body: bytes) -> dict[str, Any]:
    """정규화한 본문 잎. JSON이 아니면 본문 해시 하나(pointer "")만 남긴다(본문은 보관하지 않는다)."""
    try:
        return flatten(json.loads(body))
    except (ValueError, UnicodeDecodeError):
        return {"": hashlib.sha256(body).hexdigest()}


def _different(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    keys = sorted(set(a) | set(b))
    return [k for k in keys if a.get(k, _MISSING) != b.get(k, _MISSING)]


class TokenBucket:
    """초당 `rate`개, 최대 `rate`개까지 쌓이는 토큰. 원본 재요청 상한에 쓴다."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = float(rate)
        self.clock = clock
        self.tokens = self.rate
        self.at = clock()
        self.lock = threading.Lock()

    def take(self, n: int = 1) -> bool:
        with self.lock:
            now = self.clock()
            self.tokens = min(self.rate, self.tokens + (now - self.at) * self.rate)
            self.at = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    allow_paths: tuple[str, ...]
    rate_per_sec: int = 20
    percent: int = 100
    migration_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", self.run_id
        ):
            raise ValueError("run_id가 올바르지 않습니다.")
        paths = tuple(self.allow_paths)
        if not paths or any(not isinstance(p, str) or not p.startswith("/") for p in paths):
            raise ValueError("allow_paths는 /로 시작하는 경로가 하나 이상이어야 합니다.")
        object.__setattr__(self, "allow_paths", paths)
        # 비교 1건이 원본 2번이므로 최소 2. 상한은 원본 부담을 막는 고정값.
        object.__setattr__(self, "rate_per_sec", max(2, min(100, int(self.rate_per_sec))))

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> RunSpec:
        return cls(
            run_id=doc.get("run_id"),  # type: ignore[arg-type]
            allow_paths=tuple(doc.get("allow_paths") or ()),
            rate_per_sec=int(doc.get("rate_per_sec") or 20),
            percent=int(doc.get("percent") or 100),
            migration_id=doc.get("migration_id"),
        )


@dataclass
class _Compared:
    status_origin: int
    status_target: int
    pointers: frozenset[str]
    origin_sha: str
    target_sha: str


@dataclass
class _Candidate:
    method: str
    path: str
    status_origin: int
    status_target: int
    diff: list[dict[str, str]]


@dataclass
class _Stats:
    requests: int = 0
    skipped: int = 0
    target_5xx: int = 0
    compared: list[_Compared] = field(default_factory=list)
    candidates: list[_Candidate] = field(default_factory=list)
    noise: set[str] = field(default_factory=set)
    origin_ms: list[float] = field(default_factory=list)
    target_ms: list[float] = field(default_factory=list)


def _p99(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * 0.99) - 1)], 1)


def _forward_headers(headers: dict[str, str], host: str) -> dict[str, str]:
    out = {
        k: v
        for k, v in headers.items()
        if k.lower() not in _DROP and not k.lower().startswith(_DROP_PREFIXES)
    }
    if host:
        out["Host"] = host
    return out


class ShadowRun:
    """한 번의 그림자 리허설. `handle`은 복사본 하나를 처리한다(작업 스레드에서 부른다)."""

    def __init__(
        self,
        spec: RunSpec,
        origin: Address,
        target: Address,
        fetch: Fetch,
        clock: Callable[[], float] = time.monotonic,
        target_host: str | None = None,
    ) -> None:
        self.spec, self.origin, self.target, self.fetch = spec, origin, target, fetch
        self.target_host = target_host
        self.clock = clock
        self.started = clock()
        self.active = True
        self.bucket = TokenBucket(spec.rate_per_sec, clock)
        self.stats = _Stats()
        self.lock = threading.Lock()

    def expired(self) -> bool:
        return self.clock() - self.started > MAX_RUN_SECONDS

    def _skip(self) -> None:
        with self.lock:
            self.stats.skipped += 1

    def handle(self, method: str, path: str, headers: dict[str, str]) -> None:
        with self.lock:
            self.stats.requests += 1
        allowed = any(path.startswith(p) for p in self.spec.allow_paths)
        if method != "GET" or not allowed or not self.bucket.take(2):
            self._skip()
            return
        host = strip_shadow_host(headers.get("Host") or headers.get("host") or "")
        sent = _forward_headers(headers, host)
        first = self.fetch(self.origin, path, sent)
        second = self.fetch(self.origin, path, sent)
        if first.status is None or second.status is None:
            self._skip()  # 원본에 닿지 못하면 비교할 기준이 없다
            return
        target_headers = _forward_headers(headers, self.target_host or host)
        third = self.fetch(self.target, path, target_headers)
        self._record(method, path, first, second, third)

    def _record(self, method: str, path: str, first: Reply, second: Reply, third: Reply) -> None:
        origin, again = _leaves(first.body), _leaves(second.body)
        target = _leaves(third.body)
        noise = set(_different(origin, again))
        pointers = _different(origin, target)[:MAX_POINTERS]
        status_target = third.status if third.status is not None else 0
        compared = _Compared(
            status_origin=first.status or 0,
            status_target=status_target,
            pointers=frozenset(pointers),
            origin_sha=hashlib.sha256(first.body).hexdigest(),
            target_sha=hashlib.sha256(third.body).hexdigest(),
        )
        with self.lock:
            stats = self.stats
            stats.noise |= noise
            stats.compared.append(compared)
            if third.status is None or third.status >= 500:
                stats.target_5xx += 1
            if len(stats.origin_ms) < MAX_LATENCIES:
                stats.origin_ms.append(first.ms)
                stats.target_ms.append(third.ms)
            if self._is_mismatch(compared, stats.noise) and len(stats.candidates) < MAX_CANDIDATES:
                stats.candidates.append(
                    _Candidate(
                        method=method,
                        path=path.split("?", 1)[0],
                        status_origin=compared.status_origin,
                        status_target=status_target,
                        diff=[
                            {
                                "pointer": p,
                                "origin": mask(origin.get(p, _MISSING)),
                                "target": mask(target.get(p, _MISSING)),
                            }
                            for p in pointers
                        ],
                    )
                )

    @staticmethod
    def _is_mismatch(item: _Compared, noise: set[str]) -> bool:
        return item.status_origin != item.status_target or bool(item.pointers - noise)

    def summary(self) -> dict[str, Any]:
        with self.lock:
            stats = self.stats
            noise = set(stats.noise)
            mismatch = sum(1 for c in stats.compared if self._is_mismatch(c, noise))
            samples = []
            for cand in stats.candidates:
                diff = [d for d in cand.diff if d["pointer"] not in noise]
                if cand.status_origin == cand.status_target and not diff:
                    continue
                samples.append(
                    {
                        "method": cand.method,
                        "path": cand.path,
                        "status_origin": cand.status_origin,
                        "status_target": cand.status_target,
                        "diff": diff,
                    }
                )
                if len(samples) >= MAX_SAMPLES:
                    break
            compared = len(stats.compared)
            return {
                "run_id": self.spec.run_id,
                "requests": stats.requests,
                "compared": compared,
                "match": compared - mismatch,
                "mismatch": mismatch,
                "skipped": stats.skipped,
                "target_5xx": stats.target_5xx,
                "origin_p99_ms": _p99(stats.origin_ms),
                "target_p99_ms": _p99(stats.target_ms),
                "noise_fields": sorted(noise),
                "samples": samples,
            }


class Collector:
    """실행 하나만 활성이다. 복사본은 큐에 넣고(Envoy를 기다리게 하지 않는다) 작업 스레드가 처리한다."""

    def __init__(
        self,
        addresses: Callable[[], tuple[Address, Address]],
        fetch: Fetch,
        workers: int = 4,
        queue_size: int = 256,
        target_host: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.addresses, self.fetch, self.target_host, self.clock = (
            addresses,
            fetch,
            target_host,
            clock,
        )
        self.runs: dict[str, ShadowRun] = {}
        self.active: ShadowRun | None = None
        self.lock = threading.Lock()
        self.queue: queue.Queue[tuple[ShadowRun, str, str, dict[str, str]]] = queue.Queue(
            queue_size
        )
        for _ in range(workers):
            threading.Thread(target=self._work, daemon=True).start()

    def start(self, spec: RunSpec) -> ShadowRun:
        origin, target = self.addresses()
        with self.lock:
            existing = self.runs.get(spec.run_id)
            if existing is not None and existing.active:
                return existing  # 같은 실행을 다시 켜면 그대로(재시도 안전)
            if self.active is not None:
                self.active.active = False
            run = ShadowRun(spec, origin, target, self.fetch, self.clock, self.target_host)
            self.runs[spec.run_id] = run
            self.active = run
            while len(self.runs) > MAX_RUNS:
                oldest = next(iter(self.runs))
                if self.runs[oldest] is run:
                    break
                del self.runs[oldest]
            return run

    def stop(self, run_id: str) -> bool:
        """멈췄으면 True. 이미 멈췄거나 모르는 실행이면 False(멱등)."""
        with self.lock:
            run = self.runs.get(run_id)
            if run is None or not run.active:
                return False
            run.active = False
            if self.active is run:
                self.active = None
            return True

    def active_run_id(self) -> str | None:
        with self.lock:
            return self.active.spec.run_id if self.active else None

    def summary(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            run = self.runs.get(run_id)
        return run.summary() if run else None

    def mirror(self, method: str, path: str, headers: dict[str, str]) -> None:
        with self.lock:
            run = self.active
            if run is not None and run.expired():
                run.active = False
                self.active = run = None
        if run is None:
            return
        try:
            self.queue.put_nowait((run, method, path, headers))
        except queue.Full:
            with run.lock:
                run.stats.requests += 1
                run.stats.skipped += 1

    def _process(self, item: tuple[ShadowRun, str, str, dict[str, str]]) -> None:
        run, method, path, headers = item
        if run.active:
            run.handle(method, path, headers)

    def _work(self) -> None:
        while True:
            item = self.queue.get()
            try:
                self._process(item)
            except Exception:  # 복사본 하나의 실패로 수집기를 멈추지 않는다
                LOG.exception("shadow copy failed")
            finally:
                self.queue.task_done()

    def drain(self) -> None:
        """큐에 쌓인 복사본을 지금 스레드에서 처리한다(작업 스레드 없이 쓰는 시험용)."""
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._process(item)
            finally:
                self.queue.task_done()


def http_fetch(address: Address, path: str, headers: dict[str, str]) -> Reply:
    """GET 한 번. 본문은 상한까지만 읽는다. 연결 실패·시간 초과는 status None."""
    started = time.monotonic()
    conn = http.client.HTTPConnection(address[0], address[1], timeout=UPSTREAM_TIMEOUT_S)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        data = response.read(MAX_BODY_BYTES)
        return Reply(response.status, data, (time.monotonic() - started) * 1000)
    except (OSError, http.client.HTTPException):
        return Reply(None, b"", (time.monotonic() - started) * 1000)
    finally:
        conn.close()


def _check_loopback(host: str) -> str:
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError(f"그림자 수집기는 루프백에만 엽니다: {host}")
    return host


def serve_mirror(collector: Collector, host: str = "127.0.0.1", port: int = MIRROR_PORT):
    """Envoy 복사본을 받는다. 곧바로 204로 답하고 처리는 큐에서 한다."""

    class Handler(BaseHTTPRequestHandler):
        def _take(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, MAX_BODY_BYTES))
            collector.mirror(self.command, self.path, dict(self.headers.items()))
            self.send_response(204)
            self.end_headers()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _take

        def log_message(self, *args: Any) -> None:  # 요청 경로를 로그에 남기지 않는다
            return

    server = ThreadingHTTPServer((_check_loopback(host), port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


_SUMMARY = re.compile(r"^/runs/([^/]+)/summary$")
_RUN = re.compile(r"^/runs/([^/]+)$")


def serve_control(collector: Collector, host: str = "127.0.0.1", port: int = CONTROL_PORT):
    """제어 API. 릴레이 워커(같은 호스트)만 부른다."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, doc: dict[str, Any]) -> None:
            data = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if self.path != "/runs":
                return self._send(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                spec = RunSpec.from_json(json.loads(self.rfile.read(length) or b"{}"))
            except (ValueError, TypeError, AttributeError) as exc:
                return self._send(400, {"error": str(exc)})
            try:
                collector.start(spec)
            except Exception as exc:  # noqa: BLE001 - 원본·대상 주소를 읽지 못함
                return self._send(503, {"error": f"addresses unavailable: {exc}"})
            return self._send(200, {"ok": True, "run_id": spec.run_id})

        def do_GET(self) -> None:
            match = _SUMMARY.match(self.path)
            summary = collector.summary(match[1]) if match else None
            if summary is None:
                return self._send(404, {"error": "not_found"})
            return self._send(200, summary)

        def do_DELETE(self) -> None:
            match = _RUN.match(self.path)
            if not match:
                return self._send(404, {"error": "not_found"})
            return self._send(200, {"run_id": match[1], "stopped": collector.stop(match[1])})

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((_check_loopback(host), port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def envoy_addresses(admin_url: str) -> Callable[[], tuple[Address, Address]]:
    """원본·대상 주소를 Envoy 설정(`cluster_origin`·`cluster_target`)에서 읽는다. 실행을 켤 때마다 읽는다."""

    def read() -> tuple[Address, Address]:
        from .envoy import cluster_address, http_get_json  # yaml이 필요한 모듈은 늦게 읽는다

        dump = http_get_json(f"{admin_url.rstrip('/')}/config_dump")
        origin = cluster_address(dump, "cluster_origin")
        target = cluster_address(dump, "cluster_target")
        if origin is None or target is None:
            raise RuntimeError("cluster_origin/cluster_target not found in the Envoy config")
        return origin, target

    return read


def main() -> None:  # pragma: no cover - 서버 진입점
    """환경변수: ENVOY_ADMIN_URL(기본 http://127.0.0.1:9901), TRAFFIC_TARGET_HOST(선택, Envoy와 같은 값),
    SHADOW_MIRROR_PORT(18090), SHADOW_CONTROL_PORT(18091). 둘 다 127.0.0.1에만 연다."""
    collector = Collector(
        addresses=envoy_addresses(os.environ.get("ENVOY_ADMIN_URL", "http://127.0.0.1:9901")),
        fetch=http_fetch,
        target_host=os.environ.get("TRAFFIC_TARGET_HOST") or None,
    )
    serve_mirror(collector, "127.0.0.1", int(os.environ.get("SHADOW_MIRROR_PORT", MIRROR_PORT)))
    serve_control(collector, "127.0.0.1", int(os.environ.get("SHADOW_CONTROL_PORT", CONTROL_PORT)))
    threading.Event().wait()


if __name__ == "__main__":  # pragma: no cover
    main()
