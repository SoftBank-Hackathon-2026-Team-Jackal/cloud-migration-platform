#!/usr/bin/env bash
# 중계 서버에 K3s API TCP 전달(티켓 74, ADR-007 2안 4단계)을 켠다. 서버에서 실행한다.
# 트래픽 영역(이상헌님 담당)을 Provisioning이 대행해 만든 스크립트다. 아직 실제 서버에서 돌려 보지 않았다.
#
# 경로: 데모 서버 worker → <중계 서버 사설 IP>:<16400 + 터널 IP 호스트 번호> → <Agent 터널 IP>:<kube_api_port>
#       (10.200.0.2 → 16402). TLS는 풀지 않는다.
# 하는 일:
#   1) 전달 전용 Envoy(컨테이너 envoy-kube, 관리 포트 127.0.0.1:9902)를 /etc/envoy-kube 의 LDS·CDS 파일로 띄운다.
#      사용자 경로 Envoy(envoy, 8080·9901)는 건드리지 않는다(다시 띄우지도 않는다).
#   2) peer-sync 환경 파일에 KUBE_FORWARD_BIND_IP(이 서버 사설 IP)를 넣고 peer-sync를 다시 띄운다.
#      peer-sync가 플랫폼 Agent 목록(kube_api_port)으로 리스너를 맞춘다. 폐기·미노출 Agent는 리스너가 없다.
# 먼저 할 일:
#   - 서버의 /opt/cmp/lib(cmp_traffic)이 이 기능이 들어간 커밋이어야 한다. peer-sync가 돌고 있어야 한다.
#   - 플랫폼 TUNNEL_EXPOSE_KUBE_API=true, Agent Chart tunnel.exposeKubeApi=true (티켓 73).
#   - 보안 그룹: relay-stack.yaml 의 KubeApiForwardSourceSecurityGroupId(또는 …SourceCidr)로 데모 서버만 허용(README 참고).
# 같은 호스트 네트워크의 사용자 Envoy가 hot-restart base-id 0을 쓰므로 --base-id 1(없으면 errno=98로 재시작 반복, 78).
# 되돌리기: docker rm -f envoy-kube; /etc/cmp/peer-sync.env 의 KUBE_FORWARD_* 줄을 지우고 systemctl restart peer-sync
# 사용: sudo ./enable-kube-api-forward.sh                    # 사설 IP는 IMDSv2에서 읽는다
#       sudo BIND_IP=172.31.2.84 ./enable-kube-api-forward.sh
set -euo pipefail

DIR=/etc/envoy-kube
ENV_FILE=/etc/cmp/peer-sync.env
LIB="${LIB:-/opt/cmp/lib}"
ENVOY_IMAGE="${ENVOY_IMAGE:-envoyproxy/envoy:v1.32-latest}"
# peer-sync·relay-worker와 같은 인터프리터(중계 서버의 python3는 3.9이고 pydantic이 없다, 78)
PYTHON="${PYTHON:-python3.11}"
[[ $EUID -eq 0 ]] || { echo "sudo로 실행하세요" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "$ENV_FILE 이 없습니다(peer-sync 먼저)" >&2; exit 1; }

if [[ -z "${BIND_IP:-}" ]]; then
  token="$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
  BIND_IP="$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/local-ipv4)"
fi

install -d -m 755 "$DIR"
# 사설 IP 검사(0.0.0.0·공인·터널 대역 거부)와 첫 파일(빈 목록)을 같은 코드로 만든다.
PYTHONPATH="$LIB" "$PYTHON" - "$DIR" "$BIND_IP" <<'PY'
import sys
from pathlib import Path
from cmp_traffic.kube_forward import KubeForwardFiles, render_kube_bootstrap
from cmp_traffic.peers import AgentList

directory, bind_ip = Path(sys.argv[1]), sys.argv[2]
files = KubeForwardFiles(directory, bind_ip, "10.200.0.0/24")
if not (directory / "lds.json").exists():
    files.apply(AgentList(list_version="0", agents=[]))
(directory / "bootstrap.yaml").write_text(render_kube_bootstrap())
print(f"bind {bind_ip}: bootstrap·lds·cds 준비")
PY

docker rm -f envoy-kube >/dev/null 2>&1 || true
docker run -d --name envoy-kube --restart unless-stopped --network host \
  -v "$DIR:$DIR" "$ENVOY_IMAGE" -c "$DIR/bootstrap.yaml" --base-id 1 --log-level warn >/dev/null

sed -i '/^KUBE_FORWARD_BIND_IP=/d' "$ENV_FILE"
echo "KUBE_FORWARD_BIND_IP=$BIND_IP" >> "$ENV_FILE"
systemctl restart peer-sync
sleep 5

echo "envoy-kube: $(curl -s -m 2 http://127.0.0.1:9902/ready || echo 응답 없음)"
echo "리스너: $(curl -s -m 2 http://127.0.0.1:9902/listeners | tr '\n' ' ')"
echo "사용자 경로 Envoy(9901): $(curl -s -m 2 http://127.0.0.1:9901/ready || echo 응답 없음)"
echo "peer-sync: $(systemctl is-active peer-sync)"
