# 온프레미스 Agent 설치·운영 (15, #69)

사용자 K3s에 Agent를 설치해 계속 실행하는 방법이다. Agent는 플랫폼으로 HTTPS 아웃바운드 연결만 사용한다(인바운드 포트 불필요). 승인된 배포 작업의 처리 규칙은 [공통 배포 계약 §승인된 운영 배포](onprem-shared-deployment.md#승인된-운영-배포-12-36)를 따른다. 설계 결정 H1~H8은 [15 티켓](../../tickets/provisioning/15-agent-runtime-install.md)에 있다.

## 구성

| 구성 요소 | 위치 |
| --- | --- |
| 실행 프로세스 | `python -m cmp_providers.agent_runtime` (`cmp_providers.agent_runtime.AgentDaemon`) |
| 이미지 | `infra/onprem-agent/Dockerfile` — Agent 코드 + kubectl v1.37.1 + Helm v4.3.1(공식 체크섬 고정) + `wireguard-tools`·`iproute2` + `wireguard-go` 0.0.20250522(커밋 고정, Go checksum DB 검증), 기본 UID 65532 |
| 터널 프로세스 (17) | 같은 Pod의 `tunnel` 컨테이너, `python -m cmp_providers.agent_tunnel` (`cmp_providers.agent_tunnel.TunnelManager`) |
| Chart | `infra/helm/onprem-agent` — ServiceAccount, ClusterRole/Binding, 등록 토큰 Secret, (선택) CA ConfigMap, Deployment(`agent` + `tunnel` 컨테이너) |
| 릴리스 | 태그 `agent-vX.Y.Z` → `.github/workflows/agent-release.yml`: GHCR 이미지(amd64·arm64), OCI Chart(이미지 Digest 고정), GitHub Release |

## 1. 운영자: 등록 토큰 발급

`cluster_uid`는 생략할 수 있다. 생략하면 Agent가 최초 등록 때 조회한 kube-system Namespace UID로 고정된다(H3). 토큰은 일회용이고 기본 10분(최대 1시간) 뒤 만료된다.

```bash
curl -sS -X POST https://<platform>/agent-enrollments -H "X-Enrollment-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' -d '{"owner_id":"<owner>","agent_id":"<agent-id>"}'
```

응답의 `enrollment_token`은 이 응답에서만 한 번 반환된다. `agent_id`는 소문자·숫자·`-`(최대 63자)이며 재사용할 수 없다.

## 2. 사용자: 설치

```bash
helm install cmp-agent oci://ghcr.io/softbank-hackathon-2026-team-jackal/charts/onprem-agent --version <X.Y.Z> -n cmp-system --create-namespace --set agentId=<agent-id> --set platform.url=https://<platform> --set enrollmentToken=<token>
```

- 플랫폼 인증서가 사설 CA로 서명됐다면 `--set-file platform.caBundle=ca.crt`를 더한다(Agent는 `SSL_CERT_FILE`로 신뢰한다). HTTP는 허용하지 않는다(H7).
- 첫 실행에서 Agent가 등록하고 자격 증명을 `cmp-system/cmp-agent-credential` Secret(Helm 관리 밖)에 저장한 뒤 `cmp-agent-enrollment` Secret을 지운다(H5).
- `helm upgrade` 때는 `enrollmentToken`을 비워 둔다. 자격 증명은 업그레이드로 바뀌지 않는다.

확인:

```bash
kubectl -n cmp-system get pods
kubectl -n cmp-system logs deploy/cmp-onprem-agent
```

로그는 JSON 한 줄씩이며 코드만 담는다(`registered`, `job`, `retry`, `stopped`). 운영자는 `GET /agent-admin/agents/<agent-id>`(관리자 키)로 `last_seen_at`(마지막 인증 요청 시각, epoch 초)과 폐기 여부를 본다.

## 3. 동작 규칙 (H6)

- 작업이 없으면 `idleSeconds`(기본 10초)마다 폴링하고, 작업을 처리하면 바로 다시 폴링한다.
- 플랫폼 요청 실패와 Kubernetes 접속 불가는 1→2→4…초, 최대 60초 간격으로 재시도한다.
- 401/403(폐기·잘못된 자격 증명, 거절된 등록 토큰)은 종료 코드 2로 멈춘다. Pod는 재시작되지만 같은 이유로 다시 멈춘다.
- SIGTERM을 받으면 진행 중인 Helm 단계가 끝난 뒤 종료한다. `terminationGracePeriodSeconds`(기본 960초)는 Agent의 Helm 단계 상한(900초)보다 길다. 그래도 강제 종료돼 Release가 `pending-*`로 남으면 다음 수신 때 `UNKNOWN INSPECTION_REQUIRED`로 보고하고 자동 재적용하지 않는다(12, 시한·정리는 52).
- 새 Revision이 `--helm-timeout`(기본 5분) 안에 정착하지 않으면 그 Release를 직전 `deployed` Revision으로 되돌리고 `FAILED <원인 코드>`를 보고한다. 되돌림이 끝날 때까지 한 단계가 최대 약 두 배의 시한을 쓴다. 이전 Agent가 `failed`로 남긴 Release는 다음 배포가 먼저 복원한다(51).
- Deployment는 replica 1, `Recreate` 전략이라 교체 중 두 Agent가 함께 작업하지 않는다.

## 4. 권한 (H2)

ClusterRole은 와일드카드 없이 다음만 허용한다: Namespace get/list/create(list는 17 터널 전달 대상 찾기), StorageClass get, Secret·Service·PVC·StatefulSet·Deployment의 Helm 설치·`--wait`·검사 동사, ReplicaSet·Pod·Endpoints·EndpointSlice·Event 읽기, `pods` delete(51: 실패 Revision에 묶인 Ready가 아닌 DB Pod 정리), `pods/exec` create(`pg_isready`), `services/proxy` get(앱 헬스). 배포 Namespace가 실행 시점에 생기므로 클러스터 범위 권한이 필요하다. **클러스터 전체 Secret 읽기·쓰기 권한이 포함된다.** Agent는 `cmp.dev/environment-id` 주석이 맞지 않는 기존 Namespace를 변경하지 않는다(`NAMESPACE_NOT_OWNED`).

**Network Preflight (MNP-01 N3, #51):** `networkPreflight.image`(Digest 고정, 예 `postgres@sha256:…`)를 주면 ClusterRole에 `pods` create/delete와 `pods/log` get만 추가되고 Agent가 `NETWORK_PREFLIGHT` 작업을 실행한다. 비우면(기본) 이 권한이 없고 작업은 `PREFLIGHT_NOT_CONFIGURED`로 거절된다. 검사 Pod는 복제 Namespace의 일반 Pod 한 개와 같은 노드의 읽기 전용 `hostNetwork` Pod(`ip route get`만) 한 개이며, 끝나면 지운다. Agent는 `cmp.dev/environment-id` 주석이 있는 Namespace에서만 만든다(없으면 Pod를 만들지 않고 `NAMESPACE_NOT_OWNED`). Pod 생성 권한은 클러스터 범위라 `hostNetwork` Pod도 만들 수 있다(K3s 기본에는 Pod Security 제한이 없다). 이미 클러스터 전체 Secret·`pods/exec` 권한이 있어 신뢰 범위가 크게 늘지는 않지만, 필요 없으면 비워 둔다. 허용 이미지는 `networkPreflight.allowedImages`(비우면 `image` 하나)이며 작업이 이미지를 고를 수 없다.

```yaml
networkPreflight:
  image: postgres@sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab
```

이 작업 유형은 `agent-v0.2.0` 이후 Agent에만 있다. 이전 Agent는 `FAILED UNSUPPORTED_JOB`으로 보고하고 Worker는 `UNKNOWN`/`AGENT_JOB_UNSUPPORTED`로 끝낸다(시작 차단).

## 5. 폐기·제거

```bash
curl -sS -X POST https://<platform>/agent-admin/agents/<agent-id>/revoke -H "X-Enrollment-Admin-Key: $ADMIN_KEY"
helm uninstall cmp-agent -n cmp-system
kubectl delete namespace cmp-system
```

폐기된 Agent는 모든 Agent API에서 403이다. `tunnel` 컨테이너는 터널 인터페이스를 지우고 종료 코드 2로 멈추며, 중계 서버 목록에 `revoked: true`로 남아 중계 서버가 peer를 지운다. 남은 작업은 지우지 않으며 승인 실행 Activity가 대기 한도 뒤 `UNKNOWN AGENT_OFFLINE`을 반환한다. `cmp-agent-credential`·`cmp-agent-wireguard` Secret은 Helm이 관리하지 않으므로 Namespace 삭제로 지운다. 배포한 앱·DB Namespace는 그대로 남는다.

## 6. 복구가 필요한 경우

- 등록 직후 자격 증명 저장 전에 Agent가 중단되면 토큰은 이미 사용돼 다시 쓸 수 없다. 로그가 `ENROLLMENT_REJECTED`로 멈추면 새 `agent_id`로 토큰을 다시 발급해 설치한다.
- `cmp-agent-credential`이 지워졌다면 같은 방법으로 새 Agent를 등록하고 이전 Agent는 폐기한다.
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
                                                     └─ 10.200.0.2:5432   → <DB Service>:5432
```

- **실행:** `tunnel` 컨테이너(UID 0, `NET_ADMIN`·`NET_BIND_SERVICE`만, 권한 상승 금지, 읽기 전용 루트). `hostNetwork`를 쓰지 않아 노드 LAN 인터페이스와 분리된다. `agent` 컨테이너는 15 그대로 비루트·무권한이다.
- **모드(`tunnel.mode`):** `auto`(기본) = 커널 WireGuard, 없으면 `wireguard-go`(`/dev/net/tun` hostPath 마운트). `kernel`이면 tun을 마운트하지 않는다. `userspace`는 항상 `wireguard-go`.
- **키:** 첫 실행에 비밀키를 만들어 `cmp-system/cmp-agent-wireguard` Secret(Helm 관리 밖)에만 둔다. 공개키만 `PUT /agents/<id>/tunnel/key`로 등록한다. Secret을 지우면 새 키로 교체 등록된다(터널 IP 유지).
- **설정:** `tunnel.intervalSeconds`(기본 15초)마다 `GET .../tunnel/config`를 읽어 바뀌면(설정 버전·중계 서버 키·endpoint·노출 포트) 다시 적용한다. 중계 서버 peer의 AllowedIPs는 `10.200.0.1/32`로 좁힌다.
- **전달 대상:** `cmp.dev/environment-id` 주석이 있는 Namespace의 Service를 포트로 찾는다(5432 = DB, 그 외 노출 포트 = 앱, 헬스 경로는 같은 이름 Deployment의 readinessProbe). 찾지 못하거나 둘 이상이면 그 포트를 열지 않는다.
- **감시·재연결:** handshake가 180초 넘게 없으면 peer를 다시 설정하고, 그래도 안 되면 인터페이스를 다시 만든다. 상태(handshake 시각, 터널 경유 앱 헬스·DB 확인, 원인 코드)를 매 주기 `POST .../tunnel/status`로 보고한다.
- **Pod Security:** `NET_ADMIN`과 hostPath는 Pod Security `baseline`에 맞지 않는다. Agent Namespace에 PSA를 강제하는 클러스터라면 그 Namespace만 `privileged`로 둔다(K3s 기본값은 강제하지 않음).
- **끄기:** `--set tunnel.enabled=false` (터널 없이 15 동작만).

확인:

```bash
kubectl -n cmp-system logs deploy/cmp-onprem-agent -c tunnel
kubectl -n cmp-system exec deploy/cmp-onprem-agent -c tunnel -- wg show wg0 latest-handshakes
```

로그 이벤트: `tunnel_key_created`, `tunnel_applied`(설정 버전·모드), `tunnel`(상태가 바뀔 때, 코드만), `tunnel_recover`, `retry`, `stopped`. 원인 코드: `HANDSHAKE_MISSING`(UDP 차단·중계 서버 peer 미반영·키 불일치), `RELAY_NOT_CONFIGURED`, `INVALID_TUNNEL_CONFIG`, `TUNNEL_INTERFACE_FAILED`, `FORWARDER_BIND_FAILED`, `APP_PORT_NOT_ASSIGNED`, `FORWARD_TARGET_MISSING`, `FORWARD_TARGET_AMBIGUOUS`, `APP_HEALTH_FAILED`, `DB_CONNECT_FAILED`, `KUBERNETES_UNAVAILABLE`.

UDP가 막힌 네트워크(행사장 Wi-Fi 등) 대체 터널은 아직 없다(Traffic 담당과 결정 대기). 그런 곳에서는 `HANDSHAKE_MISSING`이 보고되고 ON_PREM Traffic 인계가 막힌다.

## 검증과 미검증

- 재현: `scripts/smoke_onprem_agent_install.py`(격리 k3d·전용 kubeconfig·`k3d-cmp-*` context 필수). 결과는 [provisioning-15-k3s.json](../../docs/verification/provisioning-15-k3s.json).
- 터널(17) 재현: `scripts/smoke_onprem_agent_tunnel.py`(격리 k3d + 테스트용 WireGuard 상대). 결과는 [provisioning-17-k3s.json](../../docs/verification/provisioning-17-k3s.json). 실제 중계 서버 경로는 미검증.
- 확인하지 않은 것: NAT 뒤 원격 노트북과 실제 공개 플랫폼 주소, GHCR 실제 게시(첫 태그 push는 별도 승인), 다양한 K3s 배포판·macOS VM, 장시간 실행.
