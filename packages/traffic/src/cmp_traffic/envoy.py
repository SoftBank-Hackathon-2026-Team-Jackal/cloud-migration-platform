"""Envoy 어댑터(#62). 예선 PoC(controller.py)에서 검증한 방식을 플랫폼 포트에 맞게 옮겼다.

- 가중치 적용: RDS 파일을 원자적으로 교체(os.replace)하고, 관리 API에서 Envoy가 그 버전을
  받아들였는지 확인한다(version_text 일치 / update_rejected 증가 감시).
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contracts import Sample

FetchJson = Callable[[str], dict[str, Any]]
FetchText = Callable[[str], str]

RDS_STAT = r"http\.ingress\.rds\.local_route\.(version_text|update_rejected)$"
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


def build_route_config(
    weights: dict[str, int], version: int, target_host: str | None = None
) -> dict[str, Any]:
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
    ) -> None:
        self.rds_path = Path(rds_path)
        self.admin_url = admin_url.rstrip("/")
        self.fetch = fetch or http_get_json
        self.target_host = target_host
        self.confirm_timeout = confirm_timeout
        self.poll_interval = poll_interval
        self.version = 0

    def _rds_stats(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({"format": "json", "filter": RDS_STAT})
        data = self.fetch(f"{self.admin_url}/stats?{query}")
        return {
            item["name"].split(".")[-1]: item["value"]
            for item in data.get("stats", [])
            if "name" in item
        }

    def _next_version(self) -> int:
        """직전 번호보다 큰 번호. Activity마다 새 인스턴스가 만들어져도 1부터 다시 시작하지 않는다.

        번호가 겹치면 Envoy의 version_text가 이미 그 값이라 "적용됐다"는 확인이 무의미해진다.
        기존 파일의 번호와 현재 시각(초)보다도 크게 잡아, 파일이 없는 새 중계 서버에서도 안전하다.
        """
        floor = self.version
        try:
            floor = max(floor, int(json.loads(self.rds_path.read_text())["version_info"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return max(floor + 1, int(time.time()))

    def apply(self, weights: dict[str, int]) -> None:
        version = self._next_version()
        doc = build_route_config(weights, version, self.target_host)
        try:
            before = self._rds_stats()
        except Exception:  # noqa: BLE001 - 아직 Envoy가 없으면 기준값 없이 확인한다
            before = {}
        tmp = self.rds_path.with_name(".rds.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, self.rds_path)  # 같은 디렉터리 안에서 원자적 교체
        self.version = version

        deadline = time.monotonic() + self.confirm_timeout
        while time.monotonic() < deadline:
            try:
                current = self._rds_stats()
            except Exception:  # noqa: BLE001 - 일시 오류는 제한 시간까지 다시 본다
                current = {}
            if str(current.get("version_text")) == str(version):
                return
            if current.get("update_rejected", 0) > before.get("update_rejected", 0):
                raise RuntimeError("envoy rejected the config")
            time.sleep(self.poll_interval)
        raise RuntimeError("timeout waiting for envoy to load the config")


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
    """Envoy `/config_dump`의 static cluster `cluster_target`이 가리키는 (주소, 포트). 없으면 None.

    DNS 이름은 풀기 전 설정값 그대로라서 핸드오버의 external_host와 같은 글자로 비교할 수 있다.
    """
    for section in config_dump.get("configs", []):
        for item in section.get("static_clusters", []):
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

    def _read(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({"format": "json", "filter": CLUSTER_STAT})
        return self.fetch(f"{self.admin_url}/stats?{query}")

    def _text(self) -> str:
        query = urllib.parse.urlencode(
            {"filter": r"cluster\.cluster_(origin|target)\.upstream_rq_time"}
        )
        assert self.fetch_text is not None
        return self.fetch_text(f"{self.admin_url}/stats/prometheus?{query}")

    def _sample(self, side: str, seconds: float) -> Sample:
        first = parse_cluster_stats(self._read(), side)
        first_text = self._text() if self.fetch_text else ""
        self.sleep(seconds)
        last = parse_cluster_stats(self._read(), side)
        requests = max(0, int(last["requests"] - first["requests"]))
        if self.fetch_text:
            p99 = window_p99(first_text, self._text(), side) if requests else 0.0
        else:
            p99 = last["p99_ms"] if last["requests"] > first["requests"] else 0
        return Sample(
            requests=requests,
            failed=max(0, int(last["failed"] - first["failed"])),
            p99_ms=p99,
        )

    def sample_origin(self, seconds: float) -> Sample:
        return self._sample("origin", seconds)

    def sample_target(self, seconds: float) -> Sample:
        return self._sample("target", seconds)
