#!/usr/bin/env bash
# 이미 깔린 중계 서버의 relay-worker에 실시간 연결 경로(TRAFFIC_STREAM_PREFIXES)를 켠다(#294). 서버에서 실행한다.
#
# 왜: 채팅 앱의 /api/stream(SSE)은 오래 열려 있는 연결이라 라우트 전체 제한시간(5초)에 끊기고,
#     그 끊김이 upstream_rq_timeout으로 세어져 전환 판정이 실패로 보고 롤백한다. 이 경로는 같은 가중치로
#     나누되 제한시간을 끄고(0s) 재시도하지 않는다.
# 하는 일:
#   1) relay-worker.service에 Environment=TRAFFIC_STREAM_PREFIXES=<값> 줄을 넣는다(있으면 값만 바꾼다).
#   2) systemctl daemon-reload 뒤 relay-worker를 다시 띄운다(Envoy는 다시 띄우지 않는다).
#   라우트 파일(rds.json)은 다음 가중치 적용(다음 전환의 첫 단계·롤백) 때 새 경로가 들어간다.
#   전환 중이 아닐 때 실행한다(워커를 다시 띄우면 진행 중인 Activity는 Temporal이 재시도한다).
#
# 먼저 서버의 /opt/cmp/lib(cmp_traffic, cmp_worker)가 이 기능이 들어간 커밋이어야 한다.
# 되돌리기: relay-worker.service의 TRAFFIC_STREAM_PREFIXES 줄을 지우고 systemctl daemon-reload && systemctl restart relay-worker
# 사용: sudo ./enable-stream-routes.sh                       # 기본값 /api/stream
#       sudo TRAFFIC_STREAM_PREFIXES=/api/stream,/events ./enable-stream-routes.sh
set -euo pipefail

UNIT=/etc/systemd/system/relay-worker.service
PREFIXES="${TRAFFIC_STREAM_PREFIXES:-/api/stream}"
[[ $EUID -eq 0 ]] || { echo "sudo로 실행하세요" >&2; exit 1; }
[[ "$PREFIXES" =~ ^/[A-Za-z0-9._~/-]*(,/[A-Za-z0-9._~/-]*)*$ ]] || { echo "경로 형식이 아닙니다: $PREFIXES" >&2; exit 2; }

if grep -q '^Environment=TRAFFIC_STREAM_PREFIXES=' "$UNIT"; then
  sed -i "s|^Environment=TRAFFIC_STREAM_PREFIXES=.*|Environment=TRAFFIC_STREAM_PREFIXES=$PREFIXES|" "$UNIT"
else
  sed -i "/^Environment=TRAFFIC_RDS_PATH=/a Environment=TRAFFIC_STREAM_PREFIXES=$PREFIXES" "$UNIT"
fi
grep -q "^Environment=TRAFFIC_STREAM_PREFIXES=$PREFIXES\$" "$UNIT" \
  || { echo "relay-worker.service에 TRAFFIC_RDS_PATH 줄이 없어 넣지 못했습니다" >&2; exit 1; }
systemctl daemon-reload
systemctl restart relay-worker
sleep 2

echo "relay-worker: $(systemctl is-active relay-worker)"
echo "설정: $(grep '^Environment=TRAFFIC_STREAM_PREFIXES=' "$UNIT")"
