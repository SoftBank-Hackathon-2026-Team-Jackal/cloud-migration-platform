# 맥을 온프레미스 대상으로 연결하기 (시연 준비 절차)

발표자 맥의 k3d 클러스터를 온프레미스 이전 대상으로 데모 플랫폼에 연결하는 절차다. 일반 설치 규칙은 [Agent 설치·운영](onprem-agent-install.md)을 따르고, 이 문서는 데모 환경에서 실제로 밟은 순서와 값을 정리한다. 2026-11-07에 이 순서대로 연결해 확인했다.

비밀값(등록 토큰, ServiceAccount 토큰, `PULSE_TOKEN`, `FAULT_TOKEN`)은 화면·로그·채팅에 출력하지 않는다. 아래 명령은 값을 변수나 0600 파일로만 다룬다.

## 0. 준비물

| 항목 | 값 |
| --- | --- |
| AWS 프로필 | 데모 계정(`714066696240`)에 닿는 프로필. 예: `AWS_PROFILE=prod` (`aws sts get-caller-identity`로 계정 번호 확인) |
| 데모 서버 | CloudFormation `cmp-demo-control-plane`의 `InstanceId` 출력, SSM으로만 접속 |
| 중계 서버 | EC2 이름 태그 `cmp-demo-relay`, SSM으로만 접속. 사설 IP는 `aws ec2 describe-instances`로 확인 |
| 플랫폼 주소 | 데모 서버 `.env`의 `CMP_PUBLIC_HOST` (예: `https://<EIP>.sslip.io`) |
| 도구 | Docker Desktop, `k3d`, `kubectl`, `helm`, `python3` |

SSM 명령은 결과가 끝날 때까지 기다려야 하므로 작은 도우미를 쓰면 편하다.

```bash
# ssm.sh <instance-id> <script-file>: 스크립트를 root로 실행하고 상태·출력을 보인다
id=$(aws ssm send-command --instance-ids "$1" --document-name AWS-RunShellScript \
  --parameters "$(python3 -c 'import json,sys;print(json.dumps({"commands":[open(sys.argv[1]).read()]}))' "$2")" \
  --query Command.CommandId --output text)
until st=$(aws ssm get-command-invocation --command-id "$id" --instance-id "$1" --query Status --output text 2>/dev/null) \
  && [[ "$st" =~ ^(Success|Failed|Cancelled|TimedOut)$ ]]; do sleep 3; done
aws ssm get-command-invocation --command-id "$id" --instance-id "$1" --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
```

SSM 명령의 매개변수는 SSM 실행 기록에 남는다. 비밀값을 명령에 실어 보냈다면 시연 뒤 그 값을 교체한다.

## 1. k3d 클러스터

```bash
k3d cluster create cmp-local --servers 1 --no-lb --k3s-arg "--disable=traefik@server:0" --wait
kubectl --context k3d-cmp-local get nodes
```

Docker Desktop 메모리가 8GB 정도면 다른 k3d 클러스터와 실습 컨테이너는 멈춰 둔다(`k3d cluster stop <이름>`, `docker stop <이름>`).

## 2. 등록 토큰 발급 (데모 서버)

관리 API는 공개 에지에 열려 있지 않다. 데모 서버의 API 컨테이너 안에서 부른다. `owner_id`는 웹 설정 화면이 쓰는 값(`PLATFORM_OWNER_ID`, 기본 `platform`)과 같아야 웹에 보인다. `agent_id`는 한 번 발급하면 다시 쓸 수 없다(`409 AGENT_ID_RESERVED`). 토큰은 10분 뒤 만료되므로 3단계를 바로 이어서 한다.

```bash
# enroll.sh (데모 서버에서 실행)
docker exec -i cmp-demo-api-1 python - <<'PY'
import json, os, urllib.request
req = urllib.request.Request("http://127.0.0.1:8000/agent-enrollments", method="POST",
  data=json.dumps({"owner_id": "platform", "agent_id": "<agent-id>"}).encode(),
  headers={"X-Enrollment-Admin-Key": os.environ["AGENT_ENROLLMENT_ADMIN_KEY"], "Content-Type": "application/json"})
print("TOKEN=" + json.load(urllib.request.urlopen(req))["enrollment_token"])
PY
```

```bash
TOKEN=$(./ssm.sh <데모 서버 ID> enroll.sh | grep -o 'TOKEN=[^[:space:]]*' | cut -d= -f2-)
```

## 3. Agent 설치

GHCR의 Agent Chart·이미지(`charts/onprem-agent`, `cmp-onprem-agent`)가 공개면 [설치 문서 §2](onprem-agent-install.md#2-사용자-설치)대로 `oci://`에서 설치한다. **비공개이고 `read:packages` 토큰이 없으면** 같은 릴리스 태그의 소스로 이미지를 빌드해 k3d에 넣고 Chart도 소스로 설치한다. 빌드는 맥에서 7분쯤 걸리므로, 등록 토큰은 빌드가 끝난 뒤 발급한다.

```bash
VERSION=0.5.0   # 최신 태그: git tag -l 'agent-v*' --sort=-v:refname | head -1
git archive agent-v$VERSION | tar -x -C /tmp/agent-src
docker build -f /tmp/agent-src/infra/onprem-agent/Dockerfile -t cmp-onprem-agent:$VERSION-local /tmp/agent-src
k3d image import cmp-onprem-agent:$VERSION-local -c cmp-local

helm install cmp-agent /tmp/agent-src/infra/helm/onprem-agent --kube-context k3d-cmp-local -n cmp-system --create-namespace \
  --set agentId=<agent-id> --set platform.url=https://<CMP_PUBLIC_HOST> --set enrollmentToken="$TOKEN" \
  --set tunnel.exposeKubeApi=true \
  --set image.repository=cmp-onprem-agent --set image.tag=$VERSION-local --set image.pullPolicy=Never
unset TOKEN
```

`tunnel.exposeKubeApi=true`는 데이터 이전 worker가 터널로 이 클러스터 API에 붙는 데 필요하다(플랫폼 `TUNNEL_EXPOSE_KUBE_API=true`도 켜져 있어야 한다).

확인:

```bash
kubectl --context k3d-cmp-local -n cmp-system logs deploy/cmp-onprem-agent -c agent --tail=3    # {"event":"registered",...}
kubectl --context k3d-cmp-local -n cmp-system logs deploy/cmp-onprem-agent -c tunnel --tail=3   # last_handshake_at 값이 있다
kubectl --context k3d-cmp-local -n cmp-system exec deploy/cmp-onprem-agent -c tunnel -- wg show wg0 latest-handshakes
```

앱을 배포하기 전에는 `FORWARD_TARGET_MISSING`이 정상이다. 중계 서버에서 `wg show`로 새 peer(`allowed ips: 10.200.0.N/32`)의 handshake를 볼 수 있고, 플랫폼 `GET /agent-admin/agents`에서 `online: true`가 된다.

## 4. 데이터 이전 kubeconfig

데이터 이전 worker는 `/opt/cmp/secrets/data-migration/<ref>.yaml`로 대상 K3s에 붙는다. ref는 데모 서버 `.env`의 `WEB_START_DATA_MIGRATION.target_kubeconfig_ref`(지금은 `onprem-k3d`)다. 대상 앱 Namespace는 Agent 롤아웃이 쓰는 `cmp-app`이다. 서버 주소는 `https://<중계 서버 사설 IP>:<16400 + 터널 IP 마지막 옥텟>`이다(예: `10.200.0.5` → `16405`). 중계 서버에서 `ss -ltn | grep 1640`으로 그 포트가 열려 있는지 본다.

```bash
python3 infra/onprem-agent/make-data-kubeconfig.py apply --context k3d-cmp-local --namespace cmp-app \
  --server https://<중계 서버 사설 IP>:<포트> --ref onprem-k3d --duration 36h
# cmp-app이 아직 없어도 된다. Agent가 롤아웃에서 cmp-app을 만들 때 DB 권한을 연결한다(티켓 84).
# Agent Chart가 84 이전이면 배포 뒤 bind를 실행해야 한다. 환경을 지우고 다시 만들 때마다 필요해지므로 Chart를 올린다.
```

`--duration`을 주지 않으면 토큰이 짧게 만료된다. 시연 시각까지 남도록 준다. 파일(`~/.config/cmp/data-migration/onprem-k3d.yaml`, 0600)은 데모 서버에 root 소유 0600으로 둔다. 기존 파일은 백업한다. 확인:

```bash
docker exec cmp-demo-worker-1 kubectl --kubeconfig /run/cmp/data-migration/onprem-k3d.yaml get --raw /readyz   # ok
```

## 5. 데모 서버 `.env`

`/opt/cmp/.env`를 바꾼 뒤에는 `cd /opt/cmp/releases/$(cat /opt/cmp/current-revision) && docker compose --env-file /opt/cmp/.env -f infra/demo/compose.yaml up -d api`로 API를 다시 띄운다. 바꾸기 전에 `.env`를 백업한다.

| 키 | 값 | 이유 |
| --- | --- | --- |
| `WEB_START_DATA_MIGRATION.tunnel.target_egress_cidrs` | `["<맥의 공인 IP>/32"]` | 원본 DB 터널이 이 주소에서 오는 연결만 받는다. 맥의 공인 IP는 `curl -s https://checkip.amazonaws.com`. **행사장 등 네트워크가 바뀌면 다시 바꾼다** |
| `WEB_START_TRAFFIC.policy.stabilize_seconds` | 예: `900` | 100% 전환 뒤 안정화 구간. 없으면 장애 주입·롤백 시연을 할 수 없다 |
| `DEMO_TOOLS_ENABLED` | `true` | `/tools` 장애 스위치. 꺼져 있으면 `/api/tools/fault`가 404 |

## 6. 중계 서버 지표 보고기와 장애 스위치

원본·대상 5xx 비율·p99 보고와 장애 스위치 적용은 중계 서버의 `traffic-health` 서비스가 맡는다([traffic-health.service](../../infra/relay/traffic-health.service)).

1. `/opt/cmp/lib/cmp_traffic`을 main 커밋으로 맞춘다. 중계 서버는 배포 버킷을 읽을 수 없으므로, `git archive origin/main packages/traffic/src/cmp_traffic`를 tar.gz·base64로 SSM 명령에 실어 보낸다(약 40KB). 기존 디렉터리는 `cmp_traffic.bak-<시각>`으로 백업한다.
2. `/etc/cmp/traffic-health.env`(0600)에 `TRAFFIC_HEALTH_URL=https://<CMP_PUBLIC_HOST>/system/traffic-health/report`, `PULSE_TOKEN`(데모 서버 `PULSE_REPORT_TOKEN`과 같은 값), `FAULT_TOKEN`(새로 만든 값, `openssl rand -hex 24`)을 쓴다.
3. `PYTHONPATH=/opt/cmp/lib python3.11 -m cmp_traffic.health --dry-run`으로 Envoy를 읽는지 본다.
4. 서비스 파일을 `/etc/systemd/system/`에 두고 `systemctl enable --now traffic-health`. `journalctl -u traffic-health`에 `report status 200`이 나오고, 웹 `/api/traffic-health`가 `"fresh": true`면 된다.

대상 앱은 같은 `FAULT_TOKEN`을 환경 변수로 받아야 장애 스위치가 열린다. 이전을 시작할 때 웹에 올리는 `.env`에 `FAULT_TOKEN=<값>` 한 줄을 더한다. 플랫폼은 데모앱 저장소 main에서 이미지를 빌드하므로 장애 스위치 코드는 새 이전에 자동으로 들어간다.

## 7. 시연 전 점검

- 맥: `kubectl --context k3d-cmp-local -n cmp-system get pods` 2/2 Running, 터널 handshake가 최근이다.
- 플랫폼: 웹 설정 화면에서 대상 Agent가 온라인이고, 이전 설정에서 이 Agent를 고른다.
- `/api/traffic-health`가 `fresh: true`, `/api/tools/fault`가 200이다.
- 안정화 구간 판정에는 대상 앱 `/api/`로 관찰 창마다 `min_requests` 이상 요청이 필요하다. 시연 중 트래픽 생성기를 켜 둔다.
- 행사장 네트워크로 옮겼다면 5단계의 `target_egress_cidrs`를 다시 바꾼다. UDP가 막힌 Wi-Fi에서는 터널이 붙지 않는다(`HANDSHAKE_MISSING`).

## 8. 정리

```bash
helm uninstall cmp-agent -n cmp-system --kube-context k3d-cmp-local
k3d cluster delete cmp-local
```

플랫폼에서 Agent를 폐기한다(`POST /agent-admin/agents/<agent-id>/revoke`, 2단계처럼 API 컨테이너 안에서). 중계 서버 `traffic-health`를 끄려면 `systemctl disable --now traffic-health`. 시연에 쓴 `FAULT_TOKEN`·`PULSE_TOKEN`은 교체한다.
