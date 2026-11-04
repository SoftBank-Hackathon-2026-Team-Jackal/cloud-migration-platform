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
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contracts import Sample

FetchJson = Callable[[str], dict[str, Any]]

RDS_STAT = r"http\.ingress\.rds\.local_route\.(version_text|update_rejected)$"
CLUSTER_STAT = r"cluster\.cluster_(origin|target)\.(upstream_rq_(total|5xx|timeout|time))$"


def http_get_json(url: str, timeout: float = 2) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


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
            "route": {"weighted_clusters": {"clusters": clusters}, "timeout": "5s"},
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

    def apply(self, weights: dict[str, int]) -> None:
        version = self.version + 1
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


class EnvoyObserver:
    """ObserverPort 구현. 관찰 창 앞뒤의 카운터 차이로 요청 수와 실패 수를 낸다."""

    def __init__(
        self,
        admin_url: str = "http://127.0.0.1:9901",
        fetch: FetchJson | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.admin_url = admin_url.rstrip("/")
        self.fetch = fetch or http_get_json
        self.sleep = sleep

    def _read(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({"format": "json", "filter": CLUSTER_STAT})
        return self.fetch(f"{self.admin_url}/stats?{query}")

    def _sample(self, side: str, seconds: float) -> Sample:
        first = parse_cluster_stats(self._read(), side)
        self.sleep(seconds)
        last = parse_cluster_stats(self._read(), side)
        return Sample(
            requests=max(0, int(last["requests"] - first["requests"])),
            failed=max(0, int(last["failed"] - first["failed"])),
            p99_ms=last["p99_ms"] if last["requests"] > first["requests"] else 0,
        )

    def sample_origin(self, seconds: float) -> Sample:
        return self._sample("origin", seconds)

    def sample_target(self, seconds: float) -> Sample:
        return self._sample("target", seconds)
