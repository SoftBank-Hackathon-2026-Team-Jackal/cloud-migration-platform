#!/usr/bin/env bash
# 중계 서버에 그림자 리허설(Provisioning 83, #362)을 준비하거나 끄거나 되돌린다. 서버에서 실행한다.
# 트래픽 영역(이상헌님 담당)을 Provisioning이 대행해 만든 스크립트다. 아직 실제 서버에서 돌려 보지 않았다.
#
# 준비(기본, `sudo ./enable-shadow.sh`):
#   1) 수집기(shadow-collector.service)를 127.0.0.1:18090·18091에만 띄운다.
#   2) bootstrap.yaml을 백업하고 정적 클러스터 `cluster_shadow`(127.0.0.1:18090)를 더한 뒤 Envoy를 다시 띄운다
#      (처음 한 번, 몇 초 끊김. 이전·전환 중이 아닐 때 실행한다). 이미 있으면 Envoy를 건드리지 않는다.
#      CDS 파일은 relay-worker가 cluster_target 하나로 덮어쓰므로 거기에 두지 않는다.
#      Envoy가 올라오지 않으면 백업으로 자동으로 되돌린다.
#   준비만으로는 아무것도 복사하지 않는다. 복사는 Workflow가 shadow_start로 켜고 shadow_stop으로 끈다
#   (라우트 파일의 GET 전용 허용 경로 라우트). 클러스터만 있고 라우트가 없으면 지금과 똑같이 동작한다.
# 끄기(`sudo ./enable-shadow.sh off`):
#   라우트 파일에서 복사 라우트를 빼고(가중치는 그대로) 수집기를 멈춘다. Envoy는 다시 띄우지 않는다.
# 되돌리기(`sudo ./enable-shadow.sh revert`):
#   끄기 + 수집기 서비스 제거 + 백업 bootstrap 복원 후 Envoy 재시작(몇 초 끊김).
#
# 먼저 서버의 /opt/cmp/lib(cmp_traffic, cmp_worker)가 이 기능이 들어간 커밋이어야 한다.
# 하지 않는 것: 보안 그룹·공개 포트 변경, 데이터베이스·대상 클러스터 변경, 쓰기 요청 복사.
set -euo pipefail

MODE="${1:-on}"
BOOTSTRAP=/etc/envoy/bootstrap.yaml
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/shadow-collector.service"
UNIT=/etc/systemd/system/shadow-collector.service
ENV_FILE=/etc/cmp/shadow-collector.env
LIB="${LIB:-/opt/cmp/lib}"
PYTHON="${PYTHON:-python3.11}"
[[ $EUID -eq 0 ]] || { echo "sudo로 실행하세요" >&2; exit 1; }

shadow_routes_off() {
  # relay-worker와 같은 TRAFFIC_* 설정으로 라우트를 다시 쓴다(실시간 경로·대상 Host 유지).
  local envs
  envs="$(systemctl show relay-worker -p Environment --value 2>/dev/null || true)"
  env $envs PYTHONPATH="$LIB" "$PYTHON" - <<'PY'
import os
from pathlib import Path
from cmp_traffic.envoy import EnvoyProxy
from cmp_worker.activities.traffic import stream_prefixes_from_env

rds = os.environ.get("TRAFFIC_RDS_PATH")
if not rds or not Path(rds).exists():
    raise SystemExit("TRAFFIC_RDS_PATH 라우트 파일이 없습니다")
proxy = EnvoyProxy(
    Path(rds),
    os.environ.get("TRAFFIC_ENVOY_ADMIN", "http://127.0.0.1:9901"),
    target_host=os.environ.get("TRAFFIC_TARGET_HOST") or None,
    stream_prefixes=stream_prefixes_from_env(),
)
print("복사 라우트:", "껐습니다" if proxy.set_shadow(None) else "이미 꺼져 있습니다")
PY
}

case "$MODE" in
  on)
    install -d -m 755 /etc/cmp
    if [[ ! -f "$ENV_FILE" ]]; then
      target_host="$(systemctl show relay-worker -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | sed -n 's/^TRAFFIC_TARGET_HOST=//p')"
      { echo "ENVOY_ADMIN_URL=http://127.0.0.1:9901"
        [[ -n "$target_host" ]] && echo "TRAFFIC_TARGET_HOST=$target_host"; } > "$ENV_FILE"
      chmod 644 "$ENV_FILE"
    fi
    install -m 644 "$UNIT_SRC" "$UNIT"
    systemctl daemon-reload
    systemctl enable --now shadow-collector
    if grep -q 'name: cluster_shadow' "$BOOTSTRAP"; then
      echo "cluster_shadow가 이미 있습니다(Envoy를 다시 띄우지 않습니다)."
    else
      backup="$BOOTSTRAP.bak-before-shadow-$(date -u +%Y%m%dT%H%M%SZ)"
      cp -p "$BOOTSTRAP" "$backup"
      PYTHONPATH="$LIB" "$PYTHON" - "$BOOTSTRAP" <<'PY'
import os, sys
from cmp_traffic.relay import add_shadow_cluster

path = sys.argv[1]
text = add_shadow_cluster(open(path).read())
tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
open(tmp, "w").write(text)
os.replace(tmp, path)
print("bootstrap에 cluster_shadow(127.0.0.1:18090)를 더했습니다")
PY
      docker restart envoy >/dev/null
      ok=""
      for _ in $(seq 1 20); do
        sleep 1
        if [[ "$(curl -s -m 2 http://127.0.0.1:9901/ready || true)" == LIVE ]] \
           && curl -s -m 2 http://127.0.0.1:9901/clusters | grep -q '^cluster_shadow::'; then
          ok=1; break
        fi
      done
      if [[ -z "$ok" ]]; then
        echo "Envoy가 cluster_shadow로 올라오지 않아 백업으로 되돌립니다." >&2
        cp -p "$backup" "$BOOTSTRAP"
        docker restart envoy >/dev/null
        exit 1
      fi
    fi
    ;;
  off)
    shadow_routes_off
    systemctl stop shadow-collector 2>/dev/null || true
    ;;
  revert)
    shadow_routes_off || echo "라우트 끄기를 하지 못했습니다(라우트 파일 확인 필요)" >&2
    systemctl disable --now shadow-collector 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    backup="$(ls -1t "$BOOTSTRAP".bak-before-shadow-* 2>/dev/null | head -1 || true)"
    if [[ -n "$backup" ]]; then
      cp -p "$backup" "$BOOTSTRAP"
      docker restart envoy >/dev/null
      echo "bootstrap을 $backup 로 되돌렸습니다"
    fi
    ;;
  *)
    echo "사용: sudo $0 [on|off|revert]" >&2; exit 2 ;;
esac

sleep 2
echo "envoy: $(curl -s -m 2 http://127.0.0.1:9901/ready || echo 응답 없음)"
echo "shadow-collector: $(systemctl is-active shadow-collector 2>/dev/null || true)"
echo "수신 포트: $(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':1809[01]$' | tr '\n' ' ')"
echo "사용자 입구(8080): $(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz || true)"
