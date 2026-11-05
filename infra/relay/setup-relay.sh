#!/usr/bin/env bash
# 중계 서버(Amazon Linux 2023) 초기 설정(#63). 시험에서 손으로 한 순서를 그대로 옮겼다.
#
# 하는 일: WireGuard 서버(peer 없이), Envoy(Docker, 호스트 네트워크), 라우트 파일 폴더 준비.
# 하지 않는 일: 개인키 출력·저장 위치 변경(키는 /etc/wireguard/priv 에만 있고 공개키만 출력한다),
#               peer 추가(peer 동기화가 플랫폼 Agent 목록으로 맞춘다), 보안 그룹 설정.
#
# 준비물(이 저장소가 있는 PC에서 만들어 서버로 복사):
#   BOOTSTRAP=bootstrap.yaml  python -c "from cmp_traffic.relay import render_bootstrap; \
#       print(render_bootstrap(('원본주소', 80), ('10.200.0.2', 8080)))" > bootstrap.yaml
#   RDS_INIT=rds.json         python -c "import json; from cmp_traffic.envoy import build_route_config; \
#       print(json.dumps(build_route_config({'origin': 100, 'target': 0}, 1)))" > rds.json
# 보안 그룹: UDP 51820(Agent 공인 IP만), TCP 8080(사용자), 관리 포트 9901은 열지 않는다.
#
# 단서: dnf·WireGuard 설정·Envoy 실행은 시험 때 같은 명령으로 확인했고, systemd 등록(wg-quick@wg0)은 아직 다시 돌려보지 않았다.
#
# 사용: BOOTSTRAP=./bootstrap.yaml RDS_INIT=./rds.json ./setup-relay.sh
set -euo pipefail

BOOTSTRAP="${BOOTSTRAP:?BOOTSTRAP=bootstrap.yaml 경로가 필요합니다}"
RDS_INIT="${RDS_INIT:?RDS_INIT=rds.json 경로가 필요합니다}"
TUNNEL_ADDR="${TUNNEL_ADDR:-10.200.0.1/24}" # 중계 서버 터널 주소(플랫폼 설정값과 같아야 한다)
WG_PORT="${WG_PORT:-51820}"
ENVOY_IMAGE="${ENVOY_IMAGE:-envoyproxy/envoy:v1.32-latest}"

sudo dnf install -y wireguard-tools docker
sudo systemctl enable --now docker

# --- WireGuard: 개인키는 이 서버에서만 만든다 ---
sudo install -d -m 700 /etc/wireguard
if ! sudo test -f /etc/wireguard/priv; then
  (umask 077 && wg genkey | sudo tee /etc/wireguard/priv >/dev/null)
fi
sudo sh -c 'wg pubkey < /etc/wireguard/priv > /etc/wireguard/pub'
# 시험에서 확인한 형태와 같게, 개인키를 설정 파일(권한 600)에 넣는다. 키 값은 화면에 나오지 않는다.
sudo sh -c 'umask 077; { echo "[Interface]"; echo "Address = $1"; echo "ListenPort = $2"; echo "MTU = 1420"; printf "PrivateKey = "; cat /etc/wireguard/priv; } > /etc/wireguard/wg0.conf' _ "$TUNNEL_ADDR" "$WG_PORT"
sudo systemctl enable --now wg-quick@wg0

# --- Envoy: 라우트 파일 폴더는 워커(relay 큐)가 쓴다 ---
sudo install -d -o "$(id -un)" /etc/envoy/rds
install -m 644 "$BOOTSTRAP" /etc/envoy/bootstrap.yaml
install -m 644 "$RDS_INIT" /etc/envoy/rds/rds.json
sudo docker rm -f envoy >/dev/null 2>&1 || true
sudo docker run -d --name envoy --restart unless-stopped --network host \
  -v /etc/envoy:/etc/envoy "$ENVOY_IMAGE" \
  -c /etc/envoy/bootstrap.yaml --log-level warn >/dev/null

sleep 3
echo "envoy admin: $(curl -s -m 3 http://127.0.0.1:9901/ready || echo 응답 없음)"
echo "중계 서버 WireGuard 공개키(Agent 등록 응답에 넣을 값):"
sudo cat /etc/wireguard/pub
