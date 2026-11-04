# 온프레미스 Agent 설치·운영 (15, #69)

사용자 K3s에 Agent를 설치해 계속 실행하는 방법이다. Agent는 플랫폼으로 HTTPS 아웃바운드 연결만 사용한다(인바운드 포트 불필요). 승인된 배포 작업의 처리 규칙은 [공통 배포 계약 §승인된 운영 배포](onprem-shared-deployment.md#승인된-운영-배포-12-36)를 따른다. 설계 결정 H1~H8은 [15 티켓](../../tickets/provisioning/15-agent-runtime-install.md)에 있다.

## 구성

| 구성 요소 | 위치 |
| --- | --- |
| 실행 프로세스 | `python -m cmp_providers.agent_runtime` (`cmp_providers.agent_runtime.AgentDaemon`) |
| 이미지 | `infra/onprem-agent/Dockerfile` — Agent 코드 + kubectl v1.37.1 + Helm v4.3.1(공식 체크섬 고정), UID 65532 |
| Chart | `infra/helm/onprem-agent` — ServiceAccount, ClusterRole/Binding, 등록 토큰 Secret, (선택) CA ConfigMap, Deployment |
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
- SIGTERM을 받으면 진행 중인 Helm 단계가 끝난 뒤 종료한다. `terminationGracePeriodSeconds`(기본 960초)는 Agent의 Helm 단계 상한(900초)보다 길다. 그래도 강제 종료돼 Release가 `pending-*`로 남으면 다음 수신 때 `UNKNOWN INSPECTION_REQUIRED`로 보고하고 자동 재적용하지 않는다(12).
- Deployment는 replica 1, `Recreate` 전략이라 교체 중 두 Agent가 함께 작업하지 않는다.

## 4. 권한 (H2)

ClusterRole은 와일드카드 없이 다음만 허용한다: Namespace get/create, StorageClass get, Secret·Service·PVC·StatefulSet·Deployment의 Helm 설치·`--wait`·검사 동사, ReplicaSet·Pod·Endpoints·EndpointSlice·Event 읽기, `pods/exec` create(`pg_isready`), `services/proxy` get(앱 헬스). 배포 Namespace가 실행 시점에 생기므로 클러스터 범위 권한이 필요하다. **클러스터 전체 Secret 읽기·쓰기 권한이 포함된다.** Agent는 `cmp.dev/environment-id` 주석이 맞지 않는 기존 Namespace를 변경하지 않는다(`NAMESPACE_NOT_OWNED`).

## 5. 폐기·제거

```bash
curl -sS -X POST https://<platform>/agent-admin/agents/<agent-id>/revoke -H "X-Enrollment-Admin-Key: $ADMIN_KEY"
helm uninstall cmp-agent -n cmp-system
kubectl delete namespace cmp-system
```

폐기된 Agent는 모든 Agent API에서 403이다. 남은 작업은 지우지 않으며 승인 실행 Activity가 대기 한도 뒤 `UNKNOWN AGENT_OFFLINE`을 반환한다. `cmp-agent-credential` Secret은 Helm이 관리하지 않으므로 Namespace 삭제로 지운다. 배포한 앱·DB Namespace는 그대로 남는다.

## 6. 복구가 필요한 경우

- 등록 직후 자격 증명 저장 전에 Agent가 중단되면 토큰은 이미 사용돼 다시 쓸 수 없다. 로그가 `ENROLLMENT_REJECTED`로 멈추면 새 `agent_id`로 토큰을 다시 발급해 설치한다.
- `cmp-agent-credential`이 지워졌다면 같은 방법으로 새 Agent를 등록하고 이전 Agent는 폐기한다.

## 검증과 미검증

- 재현: `scripts/smoke_onprem_agent_install.py`(격리 k3d·전용 kubeconfig·`k3d-cmp-*` context 필수). 결과는 [provisioning-15-k3s.json](../../docs/verification/provisioning-15-k3s.json).
- 확인하지 않은 것: NAT 뒤 원격 노트북과 실제 공개 플랫폼 주소, GHCR 실제 게시(첫 태그 push는 별도 승인), 다양한 K3s 배포판·macOS VM, 장시간 실행.
