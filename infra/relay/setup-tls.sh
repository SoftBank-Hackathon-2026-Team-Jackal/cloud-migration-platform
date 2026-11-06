#!/usr/bin/env bash
# 중계 서버(Amazon Linux 2023)에 HTTPS를 붙인다: Caddy가 <공인IP>.sslip.io 인증서를 받아 443에서 응답하고,
# 안쪽의 Envoy(사용자 포트 8080)로 넘긴다. Envoy 설정과 기존 8080 경로는 건드리지 않는다.
#
# 왜 Caddy인가: 데모 서버(edge)가 이미 같은 방식으로 sslip.io 인증서를 받는다. Envoy에 인증서 발급·갱신을
# 넣는 것보다 작고, 문제가 생기면 컨테이너 하나만 지우면 된다.
#
# 하는 일: Caddy 컨테이너(호스트 네트워크, 데모 서버와 같은 고정 이미지)를 띄운다. 인증서·계정 키는 /var/lib/cmp-caddy에만 둔다.
# 하지 않는 일: 보안 그룹(443·80은 relay-stack.yaml이 연다), Envoy 설정, 도메인 구입.
#
# 되돌리기: sudo docker rm -f caddy  (그러면 443·80은 응답하지 않고, 8080은 그대로다.)
# 사용(서버에서): ./setup-tls.sh            # 공인 IP는 인스턴스 메타데이터(IMDSv2)에서 읽는다
#                RELAY_HOST=1.2.3.4.sslip.io ./setup-tls.sh
set -euo pipefail

CADDY_IMAGE="${CADDY_IMAGE:-caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d}"
UPSTREAM="${UPSTREAM:-127.0.0.1:8080}" # Envoy 사용자 포트

if [[ -z "${RELAY_HOST:-}" ]]; then
  token="$(curl -fsS -m 5 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
  ip="$(curl -fsS -m 5 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/public-ipv4)"
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "공인 IP를 읽지 못했습니다: '$ip'" >&2; exit 1; }
  RELAY_HOST="${ip}.sslip.io"
fi
[[ "$RELAY_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "호스트 이름이 올바르지 않습니다" >&2; exit 1; }

sudo install -d -m 755 /etc/cmp-caddy
sudo install -d -m 700 /var/lib/cmp-caddy
sudo tee /etc/cmp-caddy/Caddyfile >/dev/null <<EOF
${RELAY_HOST} {
	encode zstd gzip
	reverse_proxy ${UPSTREAM}
}
EOF

sudo docker rm -f caddy >/dev/null 2>&1 || true
sudo docker run -d --name caddy --restart unless-stopped --network host \
  -v /etc/cmp-caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v /var/lib/cmp-caddy:/data \
  "$CADDY_IMAGE" >/dev/null

echo "caddy 시작: https://${RELAY_HOST} -> ${UPSTREAM}"
# 인증서 발급은 처음 요청 때 일어난다(수 초). 결과를 잠깐 기다려 본다.
for _ in $(seq 1 20); do
  code="$(curl -sS -m 6 -o /dev/null -w '%{http_code}' "https://${RELAY_HOST}/api/messages" 2>/dev/null || true)"
  [[ "$code" == "200" ]] && { echo "https 확인: 200"; exit 0; }
  sleep 3
done
echo "https 응답 없음(마지막 코드: ${code:-없음}). 로그: sudo docker logs --tail 30 caddy" >&2
exit 1
