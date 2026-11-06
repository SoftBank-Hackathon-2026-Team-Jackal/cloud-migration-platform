#!/usr/bin/env bash
# 2안 PoC 확인(티켓 69). 읽기와 k3d cmp-dm-poc 안의 임시 Pod·Secret만 만든다. 원본 DB에는 읽기 전용 SELECT 1만 보낸다.
#   MINUTES  handshake 유지 관찰 시간(기본 11분), 30초마다 기록
source "$(dirname "$0")/lib.sh"
MINUTES="${MINUTES:-11}"
gw() { "$1" -n "$NS" exec deploy/wg-gateway -c wg -- "${@:2}"; }

log "Q1/Q2 게이트웨이 상태"
gw eks sh -c 'wg show wg0 | grep -v "private key"; ip -br addr show wg0'
gw k3d sh -c 'wg show wg0 | grep -v "private key"; ip -br addr show wg0'
eks -n "$NS" logs deploy/wg-gateway | grep '^wireguard:' || true
k3d -n "$NS" logs deploy/wg-gateway | grep '^wireguard:' || true

log "Q3 터널 경유 원본 DB 연결(k3d Pod → src-db:5432 → 원본 chat-db)"
# 비밀번호는 원본 Secret에서 k3d Secret으로 파이프로만 옮긴다. 출력하지 않고 확인 뒤 지운다.
eks -n chat-source get secret chat-secrets -o jsonpath='{.data.DB_PASSWORD}' | py -c '
import json, sys
json.dump({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": "src-db-cred", "namespace": sys.argv[1],
                        "labels": {"app.kubernetes.io/managed-by": "cmp-onprem-poc"}},
           "data": {"PGPASSWORD": sys.stdin.read().strip()}}, sys.stdout)
' "$NS" | k3d apply -f - >/dev/null
overrides='{"spec":{"containers":[{"name":"pg-check","image":"docker.io/library/postgres:16",
 "env":[{"name":"PGHOST","value":"src-db"},{"name":"PGUSER","value":"chat"},{"name":"PGDATABASE","value":"chat"},
        {"name":"PGCONNECT_TIMEOUT","value":"10"},
        {"name":"PGOPTIONS","value":"-c default_transaction_read_only=on"},
        {"name":"PGPASSWORD","valueFrom":{"secretKeyRef":{"name":"src-db-cred","key":"PGPASSWORD"}}}],
 "command":["sh","-c","pg_isready && for i in 1 2 3; do s=$(date +%s%N); psql -XAt -c \"SELECT 1\" -c \"SHOW transaction_read_only\" -c \"SELECT inet_server_addr()\"; echo ms=$(( ($(date +%s%N)-s)/1000000 )); done"]}]}}'
k3d -n "$NS" run pg-check --rm -i --restart=Never --image=docker.io/library/postgres:16 \
  --overrides="$overrides" --pod-running-timeout=120s || echo "Q3 FAIL" >&2
k3d -n "$NS" delete secret src-db-cred --ignore-not-found >/dev/null

log "Q2 handshake 유지 ${MINUTES}분 관찰(30초 간격)"
fail=0
end=$(( $(date +%s) + MINUTES * 60 ))
while (( $(date +%s) < end )); do
  now=$(date +%s)
  hs="$(gw k3d wg show wg0 latest-handshakes | awk '{print $2}')"
  rx_tx="$(gw k3d wg show wg0 transfer | awk '{print $2"/"$3}')"
  if gw k3d ping -c1 -W3 10.211.0.1 >/dev/null 2>&1; then ping=ok; else ping=FAIL; fail=1; fi
  age=$(( now - ${hs:-0} ))
  (( ${hs:-0} > 0 && age <= 180 )) || fail=1
  printf '%s handshake_age=%ss rx/tx=%s ping=%s\n' "$(date -u +%H:%M:%SZ)" "$age" "$rx_tx" "$ping"
  sleep 30
done
log "원본 쪽이 본 접속 주소(NLB 클라이언트 IP 보존 여부 확인)"
gw eks wg show wg0 endpoints
(( fail == 0 )) && log "Q2 PASS" || log "Q2 FAIL"
