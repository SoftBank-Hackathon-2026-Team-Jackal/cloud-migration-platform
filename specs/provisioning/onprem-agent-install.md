# 온프레미스 Agent 설치·운영 (15, #69)

사용자 K3s에 Agent를 설치해 계속 실행하는 방법이다. Agent는 플랫폼으로 HTTPS 아웃바운드 연결만 사용한다(인바운드 포트 불필요). 승인된 배포 작업의 처리 규칙은 [공통 배포 계약 §승인된 운영 배포](onprem-shared-deployment.md#승인된-운영-배포-12-36)를 따른다. 설계 결정 H1~H8은 [15 티켓](../../tickets/provisioning/15-agent-runtime-install.md)에 있다.

## 구성

| 구성 요소 | 위치 |
| --- | --- |
| 실행 프로세스 | `python -m cmp_providers.agent_runtime` (`cmp_providers.agent_runtime.AgentDaemon`) |
| 이미지 | `infra/onprem-agent/Dockerfile` — Agent 코드 + kubectl v1.37.1 + Helm v4.3.1(공식 체크섬 고정) + `wireguard-tools`·`iproute2` + `wireguard-go` 0.0.20250522(커밋 고정, Go checksum DB 검증), 기본 UID 65532 |
| 터널 프로세스 (17) | 같은 Pod의 `tunnel` 컨테이너, `python -m cmp_providers.agent_tunnel` (`cmp_providers.agent_tunnel.TunnelManager`) |
| Chart | `infra/helm/onprem-agent` — ServiceAccount, ClusterRole/Binding, 등록 토큰 Secret, (선택) CA ConfigMap, Deployment(`agent` + `tunnel` 컨테이너) |
| 릴리스 | 태그 `agent-vX.Y.Z` → `.github/workflows/agent-release.yml`: GHCR 이미지(amd64·arm64), OCI Chart(이미지 Digest 고정), GitHub Release, 익명 pull 점검(비공개면 경고, 42) |

데모 환경에서 발표자 맥을 대상으로 연결한 실제 순서(GHCR이 비공개일 때 소스 빌드, 데이터 이전 kubeconfig, 데모 서버 `.env`, 중계 서버 지표 보고기)는 [맥을 온프레미스 대상으로 연결하기](onprem-agent-local-mac.md)에 있다.

## 1. 운영자: 등록 토큰 발급

`cluster_uid`는 생략할 수 있다. 생략하면 Agent가 최초 등록 때 조회한 kube-system Namespace UID로 고정된다(H3). 토큰은 일회용이고 기본 10분(최대 1시간) 뒤 만료된다.

```bash
curl -sS -X POST https://<platform>/agent-enrollments -H "X-Enrollment-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' -d '{"owner_id":"<owner>","agent_id":"<agent-id>"}'
```

응답의 `enrollment_token`은 이 응답에서만 한 번 반환된다. `agent_id`는 소문자·숫자·`-`(최대 63자)이며 재사용할 수 없다.

## 2. 사용자: 설치

GHCR 패키지(이미지 `cmp-onprem-agent`, Chart `charts/onprem-agent`)가 공개면 로그인 없이 설치한다(기본, H4).

```bash
helm install cmp-agent oci://ghcr.io/softbank-hackathon-2026-team-jackal/charts/onprem-agent --version <X.Y.Z> -n cmp-system --create-namespace --set agentId=<agent-id> --set platform.url=https://<platform> --set enrollmentToken=<token>
```

**패키지가 비공개일 때(인증 pull, 42 #177):** `helm install`이 401/403으로 실패하거나 Pod가 `ErrImagePull`이면 두 패키지를 읽을 수 있는 GitHub 토큰(`read:packages`)으로 Chart를 받고, 같은 Namespace에 pull Secret을 만들어 `imagePullSecrets`로 넘긴다. 토큰은 셸 기록에 남지 않게 입력받는다. Chart는 Secret 이름만 참조하고 레지스트리 자격 증명을 값으로 받지 않는다.

```bash
read -rs GHCR_TOKEN
echo "$GHCR_TOKEN" | helm registry login ghcr.io -u <github-user> --password-stdin
kubectl create namespace cmp-system
kubectl -n cmp-system create secret docker-registry cmp-agent-ghcr --docker-server=ghcr.io --docker-username=<github-user> --docker-password="$GHCR_TOKEN"
helm install cmp-agent oci://ghcr.io/softbank-hackathon-2026-team-jackal/charts/onprem-agent --version <X.Y.Z> -n cmp-system --set agentId=<agent-id> --set platform.url=https://<platform> --set enrollmentToken=<token> --set 'imagePullSecrets[0].name=cmp-agent-ghcr'
```

- `imagePullSecrets`는 Agent Pod(`agent`·`tunnel` 컨테이너) 하나에 붙는다. Agent가 실행 중 만드는 Network Preflight 검사 Pod와 배포한 앱·DB Pod에는 적용되지 않는다(검사 이미지는 `networkPreflight.image`, 앱 이미지는 [29](../../tickets/provisioning/29-image-pull-strategy.md)의 `cmp-ecr-pull`).
- 토큰을 바꾸면 Secret을 다시 만든다(`kubectl -n cmp-system create secret ... --dry-run=client -o yaml | kubectl apply -f -`). 이미 받은 이미지는 노드 캐시에 남아 있어 재시작에는 영향이 없을 수 있다.
- 공개 여부 확인(로그인 없이 토큰·manifest 조회): `uv run python scripts/check_agent_package_access.py --version <X.Y.Z>` — 둘 다 `PUBLIC`이면 종료 코드 0, 비공개면 `PRIVATE`와 1. 수동으로는 `curl -s "https://ghcr.io/token?scope=repository:softbank-hackathon-2026-team-jackal/cmp-onprem-agent:pull"`가 토큰을 주고 그 토큰으로 `GET https://ghcr.io/v2/softbank-hackathon-2026-team-jackal/cmp-onprem-agent/manifests/<X.Y.Z>`가 200이어야 한다.

- 플랫폼 인증서가 사설 CA로 서명됐다면 `--set-file platform.caBundle=ca.crt`를 더한다(Agent는 `SSL_CERT_FILE`로 신뢰한다). HTTP는 허용하지 않는다(H7).
- **HTTPS 프록시 뒤(52, C-08):** `--set proxy.https=http://<proxy>:<port>`를 더하면 `agent`·`tunnel` 컨테이너에 `HTTPS_PROXY`/`https_proxy`와 `NO_PROXY`/`no_proxy`가 들어간다. `proxy.noProxy` 기본값은 `localhost,127.0.0.1,.svc,.cluster.local,10.42.0.0/16,10.43.0.0/16`(K3s 기본 Pod·Service CIDR)이라 클러스터 API와 Service는 프록시를 거치지 않는다. 클러스터 CIDR을 바꿨다면 `proxy.noProxy`도 맞춘다. TLS 검사 프록시는 그 CA를 `platform.caBundle`에 넣는데, `SSL_CERT_FILE`은 시스템 신뢰 목록을 대체하므로 플랫폼 인증서 체인의 CA도 함께 넣는다. WireGuard(UDP)는 HTTP 프록시를 지나지 않는다(§7).
- Network Preflight 검사 이미지는 Chart 기본값(공식 `postgres` 16 alpine, Digest 고정)이라 따로 줄 값이 없다(§4).
- 첫 실행에서 Agent가 등록하고 자격 증명을 `cmp-system/cmp-agent-credential` Secret(Helm 관리 밖)에 저장한 뒤 `cmp-agent-enrollment` Secret을 지운다(H5).
- `helm upgrade` 때는 `enrollmentToken`을 비워 둔다. 자격 증명은 업그레이드로 바뀌지 않는다.

확인:

```bash
kubectl -n cmp-system get pods
kubectl -n cmp-system logs deploy/cmp-onprem-agent
```

로그는 JSON 한 줄씩이며 코드만 담는다(`registered`, `job`, `retry`, `stopped`). 운영자는 `GET /agent-admin/agents/<agent-id>`(관리자 키)로 `last_seen_at`(마지막 인증 요청 시각, epoch 초)과 폐기 여부를 본다.

## 3. 동작 규칙 (H6)

- 작업이 없으면 `idleSeconds`(기본 2초, 시연 시간 단축을 위해 10초에서 낮춤)마다 폴링하고, 작업을 처리하면 바로 다시 폴링한다.
- 플랫폼 요청 실패와 Kubernetes 접속 불가는 1→2→4…초, 최대 60초 간격으로 재시도한다.
- 401/403(폐기·잘못된 자격 증명, 거절된 등록 토큰)이면 `stopped` 로그를 한 번 남기고 **프로세스를 끝내지 않고 대기한다**(52, C-09). 플랫폼에 다시 요청하지 않고 재시작 반복도 없다. Pod는 `Running`으로 보이므로 로그로 확인하고 `helm uninstall`한다(§5). SIGTERM을 받으면 종료 코드 2로 끝난다.
- 작업은 시한 안에만 시작한다(52). claim 응답의 남은 시간이 지났으면 클러스터를 바꾸기 전에 `FAILED JOB_EXPIRED`로 보고한다. 플랫폼도 시한이 지난 작업은 주지 않는다. 그래서 Agent가 오래 꺼져 있다 돌아와도 이미 끝난 승인 실행의 배포를 하지 않는다.
- 한 번에 작업 하나를 처리한다. 대기 중인 정리 작업은 다른 작업보다 먼저 받는다(52 D10).
- SIGTERM을 받으면 진행 중인 Helm 단계가 끝난 뒤 종료한다. `terminationGracePeriodSeconds`(기본 960초)는 Agent의 Helm 단계 상한(900초)보다 길다. 그래도 강제 종료돼 Release가 `pending-*`로 남으면 다음 수신 때 `UNKNOWN INSPECTION_REQUIRED`로 보고하고 자동 재적용하지 않는다(12). Helm 잠금은 사람이나 다른 도구가 잡고 있을 수도 있어 Agent가 버려진 잠금으로 확정하지 않는다(52 D20). §6 수동 복구 또는 정리 작업(상태와 무관하게 uninstall)을 쓴다.
- 새 Revision이 `--helm-timeout`(기본 5분) 안에 정착하지 않으면 그 Release를 직전 `deployed` Revision으로 되돌리고 `FAILED <원인 코드>`를 보고한다. 되돌림이 끝날 때까지 한 단계가 최대 약 두 배의 시한을 쓴다. 이전 Agent가 `failed`로 남긴 Release는 다음 배포가 먼저 복원한다(51).
- Deployment는 replica 1, `Recreate` 전략이라 교체 중 두 Agent가 함께 작업하지 않는다.

## 4. 권한 (H2)

ClusterRole은 와일드카드 없이 다음만 허용한다: Namespace get/list/create/delete(list는 17 터널 전달 대상 찾기, delete는 52 정리 작업의 `delete_data`), StorageClass get, Secret·Service·PVC·StatefulSet·Deployment의 Helm 설치·삭제·검사 동사(get/list/create/update/patch/delete), ReplicaSet·Pod·Endpoints·EndpointSlice·Event 읽기(get/list), `pods` delete(51: 실패 Revision에 묶인 Ready가 아닌 DB Pod 정리), `pods/exec` create(`pg_isready`), `services/proxy` get(앱 헬스). `watch`는 없다(52: 51 이후 Agent가 Helm `--wait` 없이 직접 기다린다). 배포 Namespace가 실행 시점에 생기므로 클러스터 범위 권한이 필요하다. **클러스터 전체 Secret 읽기·쓰기와 Namespace 삭제 권한이 포함된다.** Agent는 `cmp.dev/environment-id` 주석이 맞지 않는 기존 Namespace를 변경·삭제하지 않는다(`NAMESPACE_NOT_OWNED`). RBAC는 라벨로 범위를 좁힐 수 없고, Agent가 Namespace마다 Role을 만들려면 자기 권한 이상을 줄 수 없어(권한 상승 방지) 같은 클러스터 권한이나 `escalate`가 필요하다. 그래서 대상 Namespace를 설치 때 미리 선언하는 Namespace 한정 모드는 후속으로 둔다(52 D16).

**Network Preflight (MNP-01 N3, #51):** `networkPreflight.image`는 기본값이 `docker.io/library/postgres@sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab`(공식 postgres 16.15-alpine3.24 multi-arch, amd64·arm64, 52 D12)다. 그래서 기본 설치에서 ClusterRole에 `pods` create/delete와 `pods/log` get이 추가되고 Agent가 `NETWORK_PREFLIGHT` 작업을 실행한다. Docker Hub에 닿지 못하는 곳은 같은 이미지를 미러에 올리고 그 Digest로 덮어쓴다. `--set networkPreflight.image=""`로 비우면 이 권한이 없고 작업은 `PREFLIGHT_NOT_CONFIGURED`로 거절된다. 검사 Pod는 복제 Namespace의 일반 Pod 한 개와 같은 노드의 읽기 전용 `hostNetwork` Pod(`ip route get`만) 한 개이며, 끝나면 지운다. Agent는 `cmp.dev/environment-id` 주석이 있는 Namespace에서만 만든다(없으면 Pod를 만들지 않고 `NAMESPACE_NOT_OWNED`). Pod 생성 권한은 클러스터 범위라 `hostNetwork` Pod도 만들 수 있다(K3s 기본에는 Pod Security 제한이 없다). 이미 클러스터 전체 Secret·`pods/exec` 권한이 있어 신뢰 범위가 크게 늘지는 않지만, 필요 없으면 비워 둔다. 허용 이미지는 `networkPreflight.allowedImages`(비우면 `image` 하나)이며 작업이 이미지를 고를 수 없다.

```yaml
networkPreflight:
  image: <mirror>/postgres@sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab  # 미러를 쓸 때만
```

이 작업 유형은 `agent-v0.2.0` 이후 Agent에만 있다. 이전 Agent는 `FAILED UNSUPPORTED_JOB`으로 보고하고 Worker는 `UNKNOWN`/`AGENT_JOB_UNSUPPORTED`로 끝낸다(시작 차단).

## 5. 폐기·제거

배포한 앱·DB를 먼저 지우려면 Agent가 살아 있을 때 정리 작업을 만든다(52). `<rollout_job_id>`는 상태 조회(`GET /provisioning/plans/<plan>/revisions/<rev>/rollout`)의 `job_id`다.

```bash
curl -sS -X POST https://<platform>/agent-admin/jobs/<rollout_job_id>/teardown -H "X-Enrollment-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' -d '{"delete_data": false}'
curl -sS https://<platform>/agent-admin/jobs/<teardown_job_id> -H "X-Enrollment-Admin-Key: $ADMIN_KEY"   # status/code
```

`delete_data: false`(기본)는 Helm Release, 플랫폼이 만든 Secret(DB 비밀번호 제외)·NetworkPolicy·ECR pull Secret을 지우고 DB PVC·DB 비밀번호 Secret·Namespace를 남긴다. 같은 environment로 다시 배포하면 데이터를 이어 쓴다. `delete_data: true`는 DB PVC와 Namespace(사용자가 넣은 Secret 포함)까지 지운다. 되돌릴 수 없다. 대기 중인 작업은 `POST /agent-admin/jobs/<job_id>/cancel`로 취소한다.

```bash
curl -sS -X POST https://<platform>/agent-admin/agents/<agent-id>/revoke -H "X-Enrollment-Admin-Key: $ADMIN_KEY"
helm uninstall cmp-agent -n cmp-system
kubectl delete namespace cmp-system
```

폐기된 Agent는 모든 Agent API에서 403이다. `agent`·`tunnel` 컨테이너는 `stopped AGENT_UNAUTHORIZED`를 남기고(터널은 인터페이스를 지운 뒤) 대기한다. 재시작 반복 없이 `helm uninstall`까지 그대로 있다(52). 중계 서버 목록에 `revoked: true`로 남아 중계 서버가 peer를 지운다. 남은 작업은 지우지 않으며, 승인 실행 Activity는 대기 한도 뒤 시작되지 않은 작업을 `EXPIRED`로 닫고 `FAILED AGENT_OFFLINE`을 반환한다. `cmp-agent-credential`·`cmp-agent-wireguard` Secret은 Helm이 관리하지 않으므로 Namespace 삭제로 지운다. 정리 작업 없이 폐기하면 배포한 앱·DB Namespace는 그대로 남는다(직접 `helm uninstall`·`kubectl delete namespace`).

## 6. 복구가 필요한 경우

- 등록 직후 자격 증명 저장 전에 Agent가 중단되면 토큰은 이미 사용돼 다시 쓸 수 없다. 로그가 `ENROLLMENT_REJECTED`로 멈추면 새 `agent_id`로 토큰을 다시 발급해 설치한다.
- `cmp-agent-credential`이 지워졌다면 같은 방법으로 새 Agent를 등록하고 이전 Agent는 폐기한다. 진행 중 Migration은 새 `agent_id`로 Plan을 다시 게시해야 한다. 같은 `agent_id`의 자격 재발급 API는 설계만 있고 후속이다(52 D15).
- 배포가 `UNKNOWN ROLLBACK_FAILED` 또는 `UNKNOWN INSPECTION_REQUIRED`로 멈췄다면(51): Agent가 작업 중이 아닌지 로그로 확인한 뒤 상태를 본다.

  ```bash
  helm history <release> -n <namespace>          # 마지막 deployed Revision 번호 확인
  kubectl get pods -n <namespace> -L controller-revision-hash
  ```

  `pending-*` Release는 `helm rollback <release> <deployed 번호> -n <namespace>`로 되돌린다. DB StatefulSet Pod가 Ready가 아니고 `controller-revision-hash`가 `kubectl get statefulset <release> -n <namespace> -o jsonpath='{.status.updateRevision}'`과 다르면 그 Pod를 지운다(`kubectl delete pod <pod> -n <namespace>`, PVC는 지우지 않는다). 그다음 새 Revision을 승인하면 Agent가 이어서 적용한다. `failed` 상태는 Agent가 다음 배포에서 직접 복원하므로 수동 조치가 필요 없다.

## 7. WireGuard 터널 (17)

노트북은 인바운드 포트를 열지 않는다. Agent가 WireGuard **클라이언트**로 중계 서버(Traffic 담당, WireGuard 서버 + Envoy)에 먼저 연결하고, 터널 IP에서만 앱 포트와 5432를 K3s Service로 전달한다. 결정 W1~W9는 [17 티켓](../../tickets/provisioning/17-agent-wireguard-tunnel.md)에 있다.

```text
중계 서버 10.200.0.1 ──WireGuard(UDP, MTU 1420)── wg0 10.200.0.2 (Agent Pod netns)
                                                     ├─ 10.200.0.2:<앱포트> → <앱 Service>:<앱포트>
                                                     ├─ 10.200.0.2:5432   → <DB Service>:5432
                                                     └─ 10.200.0.2:6443   → kubernetes.default.svc:443 (73, 켰을 때만)
```

- **실행:** `tunnel` 컨테이너(UID 0, `NET_ADMIN`·`NET_BIND_SERVICE`만, 권한 상승 금지, 읽기 전용 루트). `hostNetwork`를 쓰지 않아 노드 LAN 인터페이스와 분리된다. `agent` 컨테이너는 15 그대로 비루트·무권한이다.
- **모드(`tunnel.mode`):** `auto`(기본) = 커널 WireGuard, 없으면 `wireguard-go`(`/dev/net/tun` hostPath 마운트). `kernel`이면 tun을 마운트하지 않는다. `userspace`는 항상 `wireguard-go`.
- **키:** 첫 실행에 비밀키를 만들어 `cmp-system/cmp-agent-wireguard` Secret(Helm 관리 밖)에만 둔다. 공개키만 `PUT /agents/<id>/tunnel/key`로 등록한다. Secret을 지우면 새 키로 교체 등록된다(터널 IP 유지).
- **설정:** `tunnel.intervalSeconds`(기본 15초)마다 `GET .../tunnel/config`를 읽어 바뀌면(설정 버전·중계 서버 키·endpoint·노출 포트) 다시 적용한다. 중계 서버 peer의 AllowedIPs는 `10.200.0.1/32`로 좁힌다.
- **전달 대상:** `cmp.dev/environment-id` 주석이 있는 Namespace의 Service를 포트로 찾는다(5432 = DB, 그 외 노출 포트 = 앱, 헬스 경로는 같은 이름 Deployment의 readinessProbe). 찾지 못하거나 둘 이상이면 그 포트를 열지 않는다.
- **감시·재연결:** handshake가 180초 넘게 없으면 peer를 다시 설정하고, 그래도 안 되면 인터페이스를 다시 만든다. 상태(handshake 시각, 터널 경유 앱 헬스·DB 확인, 원인 코드)를 매 주기 `POST .../tunnel/status`로 보고한다.
- **즉시 재확인(66):** `agent` 컨테이너가 작업(롤아웃 등)을 처리할 때마다 공유 메모리 볼륨 `/var/run/cmp-wake/job`에 신호를 쓴다. tunnel은 1초마다 신호를 보고, 시작·새 신호·노출 포트 변경 뒤 120초 동안 보고가 정상(`code: null`)이 아니면 2초마다 다시 확인·보고한다. 정상이거나 120초가 지나면 평소 주기로 돌아간다. 확인 간격은 2초보다 짧지 않고, 플랫폼 오류 백오프는 그대로다. 그래서 READY 기록으로 앱 포트가 배정되면 보통 2초 안에 `app_health_ok: true`가 보고된다(이전에는 최대 한 주기). 결정 I1~I6은 [66 티켓](../../tickets/provisioning/66-tunnel-immediate-report.md)에 있다.
- **K3s API 포트(73, ADR-007 2안):** 데이터 이전 worker가 kubeconfig로 이 클러스터 API에 붙을 때만 쓴다. 플랫폼 `TUNNEL_EXPOSE_KUBE_API=true`(포트 `TUNNEL_KUBE_API_PORT`, 기본 6443)와 Chart `--set tunnel.exposeKubeApi=true` **둘 다** 켜야 열린다(기본 둘 다 꺼짐). Agent는 TLS를 풀지 않고 `kubernetes.default.svc:443`으로 TCP만 전달한다(RBAC 추가 없음). 다른 포트처럼 터널 IP에만 bind하고 중계 서버 주소에서 온 연결만 받는다. 매 주기 터널 IP:포트 TCP 연결을 확인해 `kube_api_ok`로 보고하며, 이 값은 `code`와 앱·DB 인계 판정을 바꾸지 않는다. **TLS 주의:** k3s 서버 인증서 SAN에 터널 IP·중계 서버 주소가 없다. kubeconfig는 `tls-server-name: kubernetes.default.svc`를 쓰고 `insecure-skip-tls-verify`는 쓰지 않는다. 결정 K1~K8은 [73 티켓](../../tickets/provisioning/73-agent-kube-api-port.md)에 있다.
- **Pod Security:** `NET_ADMIN`과 hostPath는 Pod Security `baseline`에 맞지 않는다. Agent Namespace에 PSA를 강제하는 클러스터라면 그 Namespace만 `privileged`로 둔다(K3s 기본값은 강제하지 않음).
- **끄기:** `--set tunnel.enabled=false` (터널 없이 15 동작만).

확인:

```bash
kubectl -n cmp-system logs deploy/cmp-onprem-agent -c tunnel
kubectl -n cmp-system exec deploy/cmp-onprem-agent -c tunnel -- wg show wg0 latest-handshakes
```

로그 이벤트: `tunnel_key_created`, `tunnel_wake_failed`(agent 컨테이너, 신호 쓰기 실패 — 평소 주기로 동작), `tunnel_applied`(설정 버전·모드), `tunnel`(상태가 바뀔 때, 코드만), `tunnel_recover`, `retry`, `stopped`. 원인 코드: `HANDSHAKE_MISSING`(UDP 차단·중계 서버 peer 미반영·키 불일치), `RELAY_NOT_CONFIGURED`, `INVALID_TUNNEL_CONFIG`, `TUNNEL_INTERFACE_FAILED`, `FORWARDER_BIND_FAILED`, `APP_PORT_NOT_ASSIGNED`, `FORWARD_TARGET_MISSING`, `FORWARD_TARGET_AMBIGUOUS`, `APP_HEALTH_FAILED`, `DB_CONNECT_FAILED`, `KUBERNETES_UNAVAILABLE`. K3s API 확인(73)은 원인 코드가 아니라 `tunnel` 이벤트의 `kube_api_ok`로 남는다.

UDP가 막힌 네트워크(행사장 Wi-Fi 등) 대체 터널은 아직 없다(Traffic 담당과 결정 대기). 그런 곳에서는 `HANDSHAKE_MISSING`이 보고되고 ON_PREM Traffic 인계가 막힌다.

## 8. 데이터 이전 kubeconfig (75, ADR-007 2안 5단계)

데이터 이전 worker가 대상 K3s에서 `kubectl exec … psql`과 데이터 게이트웨이(`cmp-data-tunnel`)를 다룰 때 쓰는 좁힌 ServiceAccount `cmp-data-migration/cmp-data-migration`과 kubeconfig를 사용자 맥에서 만든다. Agent Chart와 별개다(Chart 연동은 후속). 권한 표와 결정 R1~R11은 [75 티켓](../../tickets/provisioning/75-data-migration-kubeconfig.md)에 있다. 와일드카드와 Namespace 생성 권한이 없고, 대상 앱 Namespace에는 Pod 조회·삭제·exec, Secret get·create, NetworkPolicy만 준다.

```bash
# 1. Agent가 앱을 배포해 대상 Namespace가 생긴 뒤(없으면 kubeconfig만 쓰고 종료 코드 3 → 배포 뒤 bind)
python3 infra/onprem-agent/make-data-kubeconfig.py apply --context k3d-cmp-local \
  --namespace <대상 앱 Namespace> --server https://<중계 서버 사설 IP>:<16400+터널 IP 마지막 옥텟> --ref <kubeconfig_ref> --duration 12h
python3 infra/onprem-agent/make-data-kubeconfig.py bind --context k3d-cmp-local --namespace <ns> --wait-namespace 900
# 2. 권한이 표만큼인지(impersonation)
python3 infra/onprem-agent/make-data-kubeconfig.py verify --context k3d-cmp-local --namespace <ns>
# 3. 끝나면 SA·RBAC 삭제(토큰 즉시 무효). cmp-data-tunnel은 데이터 정리가 비운 뒤 직접 지운다
python3 infra/onprem-agent/make-data-kubeconfig.py delete --context k3d-cmp-local --namespace <ns>
```

- 서버 포트 규칙은 [74 티켓](../../tickets/provisioning/74-relay-kube-api-forward.md) R4다(예: 터널 IP 10.200.0.2 → 16402).
- 파일은 `~/.config/cmp/data-migration/<ref>.yaml`(0600)이다. 토큰은 화면에 나오지 않고 만료 시각만 나온다.
- kubeconfig는 `tls-server-name: kubernetes.default.svc`와 클러스터 CA(`kube-root-ca.crt`)를 쓴다. k3s 인증서 SAN에 터널·중계 서버 IP가 없어서다(73 K8). `insecure-skip-tls-verify`는 쓰지 않는다.
- 이 경로는 Chart `tunnel.exposeKubeApi=true`·플랫폼 `TUNNEL_EXPOSE_KUBE_API=true`(§7)와 중계 서버 전달(74)이 있어야 서버에서 닿는다. 서버 배치와 확인은 [데모 README](../../infra/demo/README.md)의 `DATA_MIGRATION_KUBECONFIG_DIR` 줄을 따른다.

## 검증과 미검증

- 재현: `scripts/smoke_onprem_agent_install.py`(격리 k3d·전용 kubeconfig·`k3d-cmp-*` context 필수). 결과는 [provisioning-15-k3s.json](../../docs/verification/provisioning-15-k3s.json).
- 터널(17) 재현: `scripts/smoke_onprem_agent_tunnel.py`(격리 k3d + 테스트용 WireGuard 상대). 결과는 [provisioning-17-k3s.json](../../docs/verification/provisioning-17-k3s.json). 실제 중계 서버 경로는 미검증.
- GHCR 게시(42): `agent-v0.3.0`은 게시됐지만 2026-11-06 기준 두 패키지 모두 익명 `PRIVATE`이다(점검 C-01). 공개 전환은 조직 패키지 설정에서 소유자가 한 번 한다(승인 대기, [42 티켓](../../tickets/provisioning/42-agent-package-access.md)). 그전에는 위 인증 pull 경로를 쓴다.
- 데이터 이전 kubeconfig(75): k3d에서 worker 실제 kubectl 경로 22단계·`verify` 49/49 확인. 중계 서버 경유는 미검증.
- 확인하지 않은 것: NAT 뒤 원격 노트북과 실제 공개 플랫폼 주소, 공개 전환 뒤 익명 설치, 다양한 K3s 배포판·macOS VM, 장시간 실행.
