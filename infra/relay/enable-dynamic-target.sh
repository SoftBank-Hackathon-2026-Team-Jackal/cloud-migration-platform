#!/usr/bin/env bash
# 이미 깔린 중계 서버(정적 대상)를 "이전이 넘긴 주소로 대상을 바꾸는" 동적 대상으로 바꾼다. 서버에서 실행한다.
#
# 하는 일:
#   1) bootstrap.yaml을 백업하고, cluster_target을 정적 설정에서 빼 CDS 파일(/etc/envoy/cds/cds.json)로 옮긴다.
#      지금의 대상 설정이 그대로 첫 CDS 값이라 트래픽이 가는 곳은 달라지지 않는다.
#   2) relay-worker에 TRAFFIC_CDS_PATH를 켠다(이전이 넘긴 주소가 터널 대역 안일 때만 대상을 바꾼다).
#   3) Envoy 컨테이너를 다시 띄우고(몇 초) 관리 포트가 살아 있고 대상 클러스터가 파일에서 올라왔는지 확인한다.
#      (대상이 지금 건강한지는 보지 않는다: 이전이 시작될 때 새 대상으로 바뀌고 그때 헬스를 확인한다.)
#      확인에 실패하면 백업 bootstrap으로 자동으로 되돌린다.
#
# 먼저 서버의 /opt/cmp/lib(cmp_traffic, cmp_worker)가 이 저장소의 같은 커밋이어야 한다.
# 되돌리기: cp /etc/envoy/bootstrap.yaml.bak-before-dynamic-* /etc/envoy/bootstrap.yaml && docker restart envoy
#          (그리고 relay-worker.service의 TRAFFIC_CDS_PATH 줄을 지우고 systemctl daemon-reload && systemctl restart relay-worker)
# 사용: sudo ./enable-dynamic-target.sh
set -euo pipefail

BOOTSTRAP=/etc/envoy/bootstrap.yaml
CDS_DIR=/etc/envoy/cds
CDS_FILE="$CDS_DIR/cds.json"
UNIT=/etc/systemd/system/relay-worker.service
LIB="${LIB:-/opt/cmp/lib}"
[[ $EUID -eq 0 ]] || { echo "sudo로 실행하세요" >&2; exit 1; }

if grep -q '^dynamic_resources:' "$BOOTSTRAP"; then
  echo "이미 동적 대상입니다."
else
  backup="$BOOTSTRAP.bak-before-dynamic-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$BOOTSTRAP" "$backup"
  install -d -m 755 "$CDS_DIR"
  PYTHONPATH="$LIB" python3.11 - "$BOOTSTRAP" "$CDS_FILE" <<'PY'
import os, sys
from cmp_traffic.relay import to_dynamic_bootstrap

bootstrap, cds_file = sys.argv[1], sys.argv[2]
new_yaml, cds_json = to_dynamic_bootstrap(open(bootstrap).read(), cds_file)
for path, text in ((cds_file, cds_json), (bootstrap, new_yaml)):
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    open(tmp, "w").write(text)
    os.replace(tmp, path)
print("bootstrap·cds 파일 준비")
PY
  docker restart envoy >/dev/null
  ok=""
  for _ in $(seq 1 20); do
    sleep 1
    if [[ "$(curl -s -m 2 http://127.0.0.1:9901/ready || true)" == LIVE ]] \
       && curl -s -m 2 http://127.0.0.1:9901/config_dump | grep -q '"dynamic_active_clusters"' \
       && curl -s -m 2 http://127.0.0.1:9901/clusters | grep -q 'cluster_target::'; then
      ok=1; break
    fi
  done
  if [[ -z "$ok" ]]; then
    echo "Envoy가 동적 대상으로 올라오지 않아 백업으로 되돌립니다." >&2
    cp -p "$backup" "$BOOTSTRAP"
    docker restart envoy >/dev/null
    exit 1
  fi
fi

if ! grep -q '^Environment=TRAFFIC_CDS_PATH=' "$UNIT"; then
  sed -i "/^Environment=TRAFFIC_RDS_PATH=/a Environment=TRAFFIC_CDS_PATH=$CDS_FILE" "$UNIT"
  systemctl daemon-reload
fi
systemctl restart relay-worker
sleep 2

echo "envoy: $(curl -s -m 2 http://127.0.0.1:9901/ready)"
echo "대상: $(curl -s -m 2 http://127.0.0.1:9901/clusters | grep -E 'cluster_target::[0-9.]+:[0-9]+::health_flags' | head -1)"
echo "relay-worker: $(systemctl is-active relay-worker)"
echo "사용자 입구(8080): $(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/messages)"
