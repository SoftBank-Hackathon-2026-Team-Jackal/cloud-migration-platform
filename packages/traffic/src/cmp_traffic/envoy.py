"""Envoy 어댑터(#62). 예선 PoC(controller.py)에서 검증한 방식을 플랫폼 포트에 맞게 옮겼다.

- 가중치 적용: RDS 파일을 원자적으로 교체(os.replace)하고, 관리 API에서 Envoy가 그 버전을
  받아들였는지 확인한다(version_text 일치 / update_rejected 증가 감시).
- 대상 주소 바꾸기: 같은 방식으로 CDS 파일(`cluster_target` 한 개)을 교체한다. 대상 클러스터를 정적 설정에
  박아 두면 이전마다 손으로 고치고 Envoy를 다시 띄워야 한다.
- 관측: 관리 API /stats의 클러스터 카운터를 관찰 창 앞뒤로 읽어 차이를 낸다.
설정 배포를 한 호스트 파일 공유로 하는 것은 데모 범위이며, 다른 호스트 배포는 xDS 서버가 필요하다.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .contracts import Sample
from .relay import CLUSTER_TYPE, _cluster

FetchJson = Callable[[str], dict[str, Any]]
FetchText = Callable[[str], str]

RDS_STAT = r"http\.ingress\.rds\.local_route\.(version_text|update_rejected)$"
CDS_STAT = r"cluster_manager\.cds\.(version_text|update_rejected)$"
CLUSTER_STAT = r"cluster\.cluster_(origin|target)\.(upstream_rq_(total|5xx|timeout|time))$"


def http_get_json(url: str, timeout: float = 2) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def http_get_text(url: str, timeout: float = 2) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


_BUCKET = re.compile(
    r'^envoy_cluster_upstream_rq_time_bucket\{envoy_cluster_name="cluster_(?P<side>\w+)",le="(?P<le>[^"]+)"\}\s+(?P<n>[0-9.]+)',
    re.MULTILINE,
)


def _buckets(text: str, side: str) -> dict[float, float]:
    out: dict[float, float] = {}
    for match in _BUCKET.finditer(text):
        if match["side"] == side:
            le = float("inf") if match["le"] == "+Inf" else float(match["le"])
            out[le] = float(match["n"])
    return out


def window_p99(before: str, after: str, side: str) -> float:
    """관찰 창 앞뒤 히스토그램 버킷의 차이로 그 창의 p99(ms)를 구한다. 샘플이 없으면 0."""
    first, last = _buckets(before, side), _buckets(after, side)
    deltas = {le: max(0.0, last[le] - first.get(le, 0.0)) for le in last}
    total = deltas.get(float("inf"), 0.0)
    if total <= 0:
        return 0.0
    need = math.ceil(total * 0.99)
    for le in sorted(deltas):
        if deltas[le] >= need:
            return 0.0 if le == float("inf") else le
    return 0.0


BLOCKED_PREFIXES = ("/admin", "/__cmp")


def check_stream_prefixes(prefixes: Sequence[str]) -> tuple[str, ...]:
    """실시간 연결 경로 검증(#294). `/`로 시작하고 막힌 경로(`/admin`·`/__cmp`)와 겹치지 않아야 한다.

    겹침은 양방향이다: `/admin/x`처럼 막힌 경로 안쪽도, `/`·`/a`처럼 막힌 경로를 품는 것도 거절한다
    (`/`를 받으면 모든 요청이 제한시간 없이 나가게 된다).
    """
    out: list[str] = []
    for prefix in prefixes:
        if not prefix.startswith("/"):
            raise ValueError(f"실시간 연결 경로는 /로 시작해야 합니다: {prefix!r}")
        if any(prefix.startswith(b) or b.startswith(prefix) for b in BLOCKED_PREFIXES):
            raise ValueError(f"실시간 연결 경로가 막힌 경로와 겹칩니다: {prefix!r}")
        if prefix not in out:
            out.append(prefix)
    return tuple(out)


def build_route_config(
    weights: dict[str, int],
    version: int,
    target_host: str | None = None,
    stream_prefixes: Sequence[str] = (),
) -> dict[str, Any]:
    """가중치 라우트 설정(RDS). `stream_prefixes`는 SSE 같은 오래 열린 연결의 경로다(#294).

    그 경로는 `/`와 같은 가중치로 나누되 제한시간을 끄고(`0s`) 재시도하지 않는다. 전체 제한시간(5초)에
    끊기면 `upstream_rq_timeout`으로 세어 전환 판정이 실패로 보고 롤백한다. 빈 값이면 출력이 예전과 같다.
    """
    streams = check_stream_prefixes(stream_prefixes)
    origin, target = weights["origin"], weights["target"]
    if origin < 0 or target < 0 or origin + target != 100:
        raise ValueError("가중치는 0 이상이고 합이 100이어야 합니다.")
    clusters: list[dict[str, Any]] = [
        {
            "name": "cluster_origin",
            "weight": origin,
            "response_headers_to_add": [{"header": {"key": "x-tp-env", "value": "origin"}}],
        },
        {
            "name": "cluster_target",
            "weight": target,
            "response_headers_to_add": [{"header": {"key": "x-tp-env", "value": "target"}}],
        },
    ]
    if target_host:
        clusters[1]["host_rewrite_literal"] = target_host
    forbidden = {"status": 403, "body": {"inline_string": "forbidden\n"}}
    routes = [  # 공개 경로로 관리·내부 경로에 접근하지 못하게 가장 먼저 막는다
        {"match": {"prefix": "/admin"}, "direct_response": forbidden},
        {"match": {"prefix": "/__cmp"}, "direct_response": forbidden},
        *(
            {
                "match": {"prefix": prefix},
                "route": {"weighted_clusters": {"clusters": clusters}, "timeout": "0s"},
            }
            for prefix in streams
        ),
        {
            "match": {"prefix": "/"},
            "route": {
                "weighted_clusters": {"clusters": clusters},
                "timeout": "5s",
                # 연결 실패는 요청이 나가기 전이라 다시 보내도 중복이 생기지 않는다.
                # 실제 WAN 시험에서 새 연결의 1~2%가 연결 제한에 걸려 오탐 롤백이 났다.
                "retry_policy": {
                    "retry_on": "connect-failure",
                    "num_retries": 2,
                    "per_try_timeout": "3s",
                },
            },
        },
    ]
    return {
        "version_info": str(version),
        "resources": [
            {
                "@type": "type.googleapis.com/envoy.config.route.v3.RouteConfiguration",
                "name": "local_route",
                "virtual_hosts": [{"name": "app", "domains": ["*"], "routes": routes}],
            }
        ],
    }


class EnvoyProxy:
    """ProxyPort 구현. 설정을 쓰고 Envoy가 받아들였다고 확인될 때만 돌아온다."""

    def __init__(
        self,
        rds_path: Path,
        admin_url: str = "http://127.0.0.1:9901",
        fetch: FetchJson | None = None,
        target_host: str | None = None,
        confirm_timeout: float = 2.5,
        poll_interval: float = 0.05,
        cds_path: Path | None = None,
        stream_prefixes: Sequence[str] = (),
    ) -> None:
        self.rds_path = Path(rds_path)
        self.stream_prefixes = check_stream_prefixes(stream_prefixes)
        self.cds_path = Path(cds_path) if cds_path else None
        self.admin_url = admin_url.rstrip("/")
        self.fetch = fetch or http_get_json
        self.target_host = target_host
        self.confirm_timeout = confirm_timeout
        self.poll_interval = poll_interval
        self.version = 0

    def _stats(self, pattern: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"format": "json", "filter": pattern})
        data = self.fetch(f"{self.admin_url}/stats?{query}")
        return {
            item["name"].split(".")[-1]: item["value"]
            for item in data.get("stats", [])
            if "name" in item
        }

    def _rds_stats(self) -> dict[str, Any]:
        return self._stats(RDS_STAT)

    def _next_version(self, path: Path | None = None) -> int:
        """직전 번호보다 큰 번호. Activity마다 새 인스턴스가 만들어져도 1부터 다시 시작하지 않는다.

        번호가 겹치면 Envoy의 version_text가 이미 그 값이라 "적용됐다"는 확인이 무의미해진다.
        기존 파일의 번호와 현재 시각(초)보다도 크게 잡아, 파일이 없는 새 중계 서버에서도 안전하다.
        """
        floor = self.version
        try:
            floor = max(floor, int(json.loads((path or self.rds_path).read_text())["version_info"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return max(floor + 1, int(time.time()))

    def _publish(self, path: Path, doc: dict[str, Any], version: int, pattern: str) -> None:
        """파일을 원자적으로 교체하고 Envoy가 그 버전을 받아들였다고 확인될 때만 돌아온다."""
        try:
            before = self._stats(pattern)
        except Exception:  # noqa: BLE001 - 아직 Envoy가 없으면 기준값 없이 확인한다
            before = {}
        tmp = path.with_name("." + path.stem + ".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)  # 같은 디렉터리 안에서 원자적 교체
        self.version = version

        deadline = time.monotonic() + self.confirm_timeout
        while time.monotonic() < deadline:
            try:
                current = self._stats(pattern)
            except Exception:  # noqa: BLE001 - 일시 오류는 제한 시간까지 다시 본다
                current = {}
            if str(current.get("version_text")) == str(version):
                return
            if current.get("update_rejected", 0) > before.get("update_rejected", 0):
                raise RuntimeError("envoy rejected the config")
            time.sleep(self.poll_interval)
        raise RuntimeError("timeout waiting for envoy to load the config")

    def apply(self, weights: dict[str, int]) -> None:
        version = self._next_version()
        doc = build_route_config(weights, version, self.target_host, self.stream_prefixes)
        self._publish(self.rds_path, doc, version, RDS_STAT)

    def target_weight(self) -> int | None:
        """지금 라우트 파일에서 대상으로 가는 가중치(%). 읽지 못하면 None."""
        try:
            doc = json.loads(self.rds_path.read_text(encoding="utf-8"))
            hosts = doc["resources"][0]["virtual_hosts"]
            for route in hosts[0]["routes"]:
                for cluster in (
                    route.get("route", {}).get("weighted_clusters", {}).get("clusters", [])
                ):
                    if cluster.get("name") == "cluster_target":
                        return int(cluster["weight"])
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return None
        return None

    def set_target(self, host: str, port: int, health_path: str = "/health") -> None:
        """대상 클러스터의 주소를 바꾼다(CDS 파일 교체). 중계 서버가 동적 대상으로 깔려 있어야 한다.

        Envoy가 그 버전을 받아들였다고 확인될 때만 돌아온다. 새 대상이 실제로 쓰이는지(주소가 활성인지)는
        호출하는 쪽이 `EnvoyObserver.target_address()`로 한 번 더 본다.
        """
        if self.cds_path is None:
            raise RuntimeError("dynamic target is not configured (no CDS path)")
        version = self._next_version(self.cds_path)
        doc = {
            "version_info": str(version),
            "resources": [
                {"@type": CLUSTER_TYPE, **_cluster("cluster_target", host, port, health_path)}
            ],
        }
        self._publish(self.cds_path, doc, version, CDS_STAT)


def parse_cluster_stats(data: dict[str, Any], side: str) -> dict[str, float]:
    """/stats JSON에서 한 쪽(origin|target) 클러스터의 요청·실패·p99를 뽑는다."""
    marker = f"cluster_{side}."
    counters: dict[str, float] = {}
    p99 = 0.0
    for item in data.get("stats", []):
        if "name" in item and marker in item["name"]:
            counters[item["name"].split(".")[-1]] = item["value"]
        elif "histograms" in item:
            hist = item["histograms"]
            quantiles = hist.get("supported_quantiles", [])
            index = quantiles.index(99) if 99 in quantiles else None
            for computed in hist.get("computed_quantiles", []):
                if marker in computed["name"] and computed["name"].endswith("upstream_rq_time"):
                    value = computed["values"][index].get("interval") if index is not None else None
                    if isinstance(value, int | float) and not math.isnan(value):
                        p99 = float(value)
    return {
        "requests": counters.get("upstream_rq_total", 0),
        "failed": counters.get("upstream_rq_5xx", 0) + counters.get("upstream_rq_timeout", 0),
        "p99_ms": p99,
    }


def cluster_target_address(config_dump: dict[str, Any]) -> tuple[str, int] | None:
    """Envoy `/config_dump`의 `cluster_target`이 가리키는 (주소, 포트). 없으면 None.

    정적(static_clusters)이든 파일로 바꾸는 동적(dynamic_active_clusters, 이미 활성화된 것만)이든 읽는다.
    DNS 이름은 풀기 전 설정값 그대로라서 핸드오버의 external_host와 같은 글자로 비교할 수 있다.
    """
    for section in config_dump.get("configs", []):
        for item in [
            *section.get("static_clusters", []),
            *section.get("dynamic_active_clusters", []),
        ]:
            cluster = item.get("cluster", {})
            if cluster.get("name") != "cluster_target":
                continue
            for group in cluster.get("load_assignment", {}).get("endpoints", []):
                for entry in group.get("lb_endpoints", []):
                    socket = entry.get("endpoint", {}).get("address", {}).get("socket_address", {})
                    if socket.get("address") and socket.get("port_value"):
                        return str(socket["address"]), int(socket["port_value"])
    return None


class EnvoyObserver:
    """ObserverPort 구현. 관찰 창 앞뒤의 카운터 차이로 요청 수와 실패 수를 낸다."""

    def __init__(
        self,
        admin_url: str = "http://127.0.0.1:9901",
        fetch: FetchJson | None = None,
        fetch_text: FetchText | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.admin_url = admin_url.rstrip("/")
        # 직접 주입하지 않은 실제 연결에서는 Prometheus 버킷으로 창 p99를 계산한다.
        self.fetch_text = fetch_text or (http_get_text if fetch is None else None)
        self.fetch = fetch or http_get_json
        self.sleep = sleep

    def target_address(self) -> tuple[str, int] | None:
        """이 중계 서버의 Envoy가 대상으로 보내는 곳. 읽지 못하면 None."""
        try:
            return cluster_target_address(self.fetch(f"{self.admin_url}/config_dump"))
        except (OSError, ValueError):
            return None

    def target_healthy(self) -> bool | None:
        """대상 클러스터에 Envoy 헬스 체크를 통과한 멤버가 있는지. 읽지 못하면 None(모름)."""
        query = urllib.parse.urlencode(
            {"format": "json", "filter": r"cluster\.cluster_target\.membership_healthy$"}
        )
        try:
            stats = self.fetch(f"{self.admin_url}/stats?{query}").get("stats", [])
        except (OSError, ValueError):
            return None
        for item in stats:
            if item.get("name") == "cluster.cluster_target.membership_healthy":
                return int(item.get("value") or 0) > 0
        return None

    def _read(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({"format": "json", "filter": CLUSTER_STAT})
        return self.fetch(f"{self.admin_url}/stats?{query}")

    def _text(self) -> str:
        query = urllib.parse.urlencode(
            {"filter": r"cluster\.cluster_(origin|target)\.upstream_rq_time"}
        )
        assert self.fetch_text is not None
        return self.fetch_text(f"{self.admin_url}/stats/prometheus?{query}")

    def _window_sample(
        self,
        side: str,
        first: dict[str, Any],
        last: dict[str, Any],
        first_text: str,
        last_text: str | None,
    ) -> Sample:
        before = parse_cluster_stats(first, side)
        after = parse_cluster_stats(last, side)
        requests = max(0, int(after["requests"] - before["requests"]))
        if self.fetch_text:
            p99 = window_p99(first_text, last_text or "", side) if requests else 0.0
        else:
            p99 = after["p99_ms"] if after["requests"] > before["requests"] else 0
        return Sample(
            requests=requests,
            failed=max(0, int(after["failed"] - before["failed"])),
            p99_ms=p99,
        )

    def sample_pair(self, seconds: float) -> tuple[Sample, Sample]:
        """한 관찰 창에서 (원본, 대상)을 같은 앞뒤 값으로 함께 잰다.

        원본 요청 수가 있어야 "사용자 트래픽이 없다"와 "가중치가 안 먹었다"를 가른다(#idle-probe).
        """
        first = self._read()
        first_text = self._text() if self.fetch_text else ""
        self.sleep(seconds)
        last = self._read()
        last_text = self._text() if self.fetch_text else None
        return (
            self._window_sample("origin", first, last, first_text, last_text),
            self._window_sample("target", first, last, first_text, last_text),
        )

    def _sample(self, side: str, seconds: float) -> Sample:
        origin, target = self.sample_pair(seconds)
        return origin if side == "origin" else target

    def sample_origin(self, seconds: float) -> Sample:
        return self._sample("origin", seconds)

    def sample_target(self, seconds: float) -> Sample:
        return self._sample("target", seconds)
