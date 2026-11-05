# Ticket 15 — 온프레미스 Agent 상시 실행과 설치 패키지

## 상태
DONE — [Issue #69](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/69). 착수 결정 H1~H8, 격리 k3d 실설치 검증(아래). 자체 리뷰 완료, PR 리뷰 대기.

## 목적
04A·12에서 만든 Agent(등록, 1회 폴링, 승인 배포)를 사용자 K3s에 설치해 계속 실행되는 운영 Agent로 만든다. 지금은 저장소 checkout, Python, kubectl, helm을 갖춘 호스트에서 `poll-once` CLI를 사람이 직접 실행해야 한다.

## 관련 요구사항
PROV-009, PROV-016, PROV-017, PROV-021

## 선행 작업
Blocked by: 04A, 12 (DONE). 16·17의 터널 기능이 이 실행 기반 위에 올라간다.

## 현재 구현과 재사용 지점
- `cmp_providers.remote_agent`: `enroll`/`poll-once` CLI, `AgentControlClient`(리다이렉트 거절, 산출물 다운로드)
- `cmp_providers.onprem_agent.RolloutAgent.poll_once`: 한 번 폴링 → 배포 → 보고. 주석상 반복·감독은 호출자 책임
- `ClusterTarget`: 명시 kubeconfig 파일·context 필수
- `cmp_api.agent_registry`: 소유자·등록 토큰·등록 Agent·작업 테이블. 등록 토큰 발급 시 `cluster_uid`가 필요하다
- 폐기·교체 기능 없음, Agent 온라인 여부 기록 없음

## 작업 범위
- 포함
  - 등록과 반복 폴링을 하나의 장기 실행 프로세스로 합친다: 최초 실행 시 등록 토큰으로 등록하고 자격 증명을 보관, 이후 폴링 루프(지수 백오프, 종료 신호 처리, 작업 처리 중 중복 실행 방지).
  - 컨테이너 이미지(Agent 코드 + 고정 버전 kubectl·helm)와 설치 Helm Chart(`infra/helm/onprem-agent`)를 만든다.
  - 클러스터 안에서 실행: ServiceAccount 기반 접근으로 `ClusterTarget`의 in-cluster 모드를 추가한다. kube-system UID는 Agent가 직접 조회한다.
  - 자격 증명은 Agent Namespace의 Kubernetes Secret에 보관한다. 등록 토큰은 등록 후 Secret에서 제거한다.
  - 플랫폼에 마지막 접속 시각을 기록하고(폴링 시 갱신) 운영자 상태 조회에 노출한다.
  - 운영자 자격 증명 폐기 API. 폐기된 Agent의 폴링·보고·산출물 다운로드는 거절한다.
  - 릴리스 워크플로(`.github/workflows/agent-release.yml`): 태그 기준 이미지·Chart 빌드와 GitHub Release 생성(H4).
  - 설치 절차 문서(필요 값, 명령, 확인 방법, 제거 방법).
- 제외: WireGuard·터널(16·17), 웹 등록 화면, 공개 회원가입, Planning의 ON_PREM Plan 게시 연결, 자동 업데이트.

## 수용 기준
- [x] 설치 Chart 하나와 등록 토큰·플랫폼 URL만으로 Agent가 등록되고, 재시작 후에도 재등록 없이 같은 자격 증명으로 폴링을 이어 간다.
- [x] Agent Pod가 재시작·재스케줄돼도 RUNNING 작업을 다시 받아 12의 재수신 규칙대로 처리하고 중복 배포하지 않는다.
- [x] 플랫폼 접속 불가 시 백오프하며 계속 시도하고, 복구 후 대기 작업을 처리한다.
- [x] 폐기된 자격 증명은 모든 Agent API에서 거절되고, 운영자는 마지막 접속 시각으로 온라인 여부를 볼 수 있다.
- [x] 이미지·Chart·로그·Secret 외 저장소에 등록 토큰과 자격 증명 원문이 남지 않는다.
- [x] 기존 04·04A·12 테스트 회귀 없음.

## Test Seam
- (1) 장기 실행 루프를 가짜 `AgentControlClient`·가짜 시계로 검사: 백오프, 종료 신호, 처리 중 중복 방지, 재시작 후 자격 증명 재사용.
- (2) FastAPI `TestClient` + SQLite: 마지막 접속 시각 갱신, 폐기 후 거절.
- (3) 격리 k3d에 실제 이미지·Chart 설치 → 등록 → 승인 배포 1건 처리 → Pod 삭제 후 재시작 → 재등록 없이 이어서 폴링.
- 놓치는 것: 실제 NAT·HTTPS 원격 연결(17과 함께 별도 기록), 다양한 K3s 배포판, macOS 위 K3s VM.

## 착수 결정 (2026-11-05 확정)

사용자 지시: 기획 문서의 최신 결정을 따르고, 없는 항목은 구현자가 최선안을 골라 기록한다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| H1 실행 위치 | 사용자 K3s의 `cmp-system` Namespace(`helm install -n cmp-system --create-namespace`)에 Deployment replica 1, `strategy: Recreate`로 실행한다. 호스트 실행(kubeconfig + context) CLI도 유지한다 | 호스트 OS와 무관. Recreate로 교체 중 두 Agent가 동시에 작업을 처리하지 않는다. 17의 WireGuard도 같은 Pod에 둔다 |
| H2 권한 | ClusterRole에 필요한 자원·동사만 명시한다(와일드카드 금지): Namespace get/create, StorageClass get, 관리 대상 자원(Secret·Service·PVC·Pod·Endpoints·StatefulSet·Deployment·ReplicaSet·Event)의 Helm 설치·`--wait`·검사에 필요한 동사, `pods/exec` create, `services/proxy` get. 대상 Namespace가 동적으로 생기므로 ClusterRole이 필요하고, 다른 Namespace 침범은 12의 `NAMESPACE_NOT_OWNED` 코드 검사로 막는다 | 대안: 관리 Namespace마다 RoleBinding을 Agent가 만드는 방식은 RoleBinding 생성 권한으로 결국 같은 범위를 얻어 이점이 작다. 클러스터 전체 Secret 접근 권한이 남는 점을 설치 문서에 명시한다 |
| H3 클러스터 바인딩 | 발급 시 `cluster_uid`는 선택. 비우면 최초 등록 때 Agent가 조회해 보고한 kube-system UID로 고정한다. 지정했다면 기존처럼 일치 검사 | 사용자가 UID를 조회해 운영자에게 전달하는 단계를 없앤다. 04A 계약 변경 → `onprem-shared-deployment.md` 갱신 |
| H4 배포 방식 (**확정 2026-11-05**) | GitHub Release로 버전 배포. 태그 `agent-v*` push 시 Actions가 amd64·arm64 이미지를 GHCR에 push하고, Chart를 OCI(`oci://ghcr.io/<org>/charts/onprem-agent`)로 올리며 Release에 `.tgz`·이미지 Digest·설치 명령을 남긴다. Chart는 이미지를 Digest로 고정한다. 저장소는 공개될 예정이므로 공개 패키지를 전제로 한다(인증 없는 설치). 검증은 k3d 이미지 import와 워크플로 dry-run, 첫 태그 push·공개 전환은 별도 승인 | 사용자 결정. 사용자 클러스터가 플랫폼 ECR 인증 없이 받을 수 있어야 하고, 배포한 Agent 버전을 Digest로 추적한다 |
| H5 자격 증명 보관 | Chart는 등록 토큰만 Secret `cmp-agent-enrollment`로 만든다. Agent는 등록 성공 후 자격 증명을 Helm이 관리하지 않는 Secret `cmp-agent-credential`에 쓰고 토큰 Secret을 지운다. 자격 증명이 있으면 토큰은 무시한다. 호스트 CLI는 기존 0600 파일 방식 | `helm upgrade`가 자격 증명을 덮어쓰지 않는다. 등록 직후 저장 전 중단되면 토큰은 이미 사용됐으므로 새 토큰 발급이 필요하다(문서화) |
| H6 실행 루프 | 작업이 없으면 10초 대기, 작업을 처리하면 즉시 다시 폴링, 플랫폼 요청 실패는 지수 백오프(최대 60초), 401/403은 `AGENT_UNAUTHORIZED`로 종료(비정상 종료 코드). SIGTERM은 진행 중 작업을 마친 뒤 종료하고 `terminationGracePeriodSeconds`를 Helm 대기 시간보다 길게 둔다 | 폐기된 Agent가 계속 요청하지 않게 한다. 강제 종료로 Release가 `pending-*`에 남으면 12 규칙대로 `UNKNOWN INSPECTION_REQUIRED` |
| H7 플랫폼 TLS | localhost가 아니면 HTTPS 필수(기존 규칙 유지). 사설 CA는 Chart 값 `platform.caBundle`로 받아 `SSL_CERT_FILE`로 신뢰한다. HTTP 예외 옵션은 만들지 않는다 | k3d 검증은 자체 서명 CA로 실제 TLS 경로를 확인한다 |
| H8 마지막 접속·폐기 | 인증된 Agent 요청마다 `last_seen_at`을 갱신한다. 운영자 API `GET /agent-admin/agents/{agent_id}`(상태)와 `POST /agent-admin/agents/{agent_id}/revoke`(폐기, 관리자 키). 폐기된 Agent는 모든 Agent API에서 403이고 Agent ID는 재사용하지 않는다. 남은 작업은 지우지 않으며 12 Activity가 대기 한도 후 `UNKNOWN AGENT_OFFLINE`을 반환한다 | 작업 상태 원천을 보존한다 |

## 완료 시 확인할 동작
사용자가 Chart로 Agent를 설치하면 계속 실행되면서 승인된 배포 작업을 받아 처리하고, 재시작·일시 단절 뒤에도 이어서 동작한다.

## 관련 문서
- [04A](04a-onprem-agent-enrollment.md) · [12](12-k3s-approved-rollout.md) · [공통 배포 계약](../../specs/provisioning/onprem-shared-deployment.md) · [k3s-agent](../../specs/provisioning/k3s-agent.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 2026-11-05 사용자 결정: Provisioning 범위는 외부 연결이 가능한 온프레미스 Agent까지다. 중계 서버 내부(Envoy 라우팅·가중치, DNS)는 Traffic 담당이다. 이 티켓은 그중 실행 기반을 맡는다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰 (자체 리뷰; PR 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `make check` (ruff + pytest, macOS, Python 3.11, Helm v4.3.1) | 625 passed |
| `uv run pytest tests/test_agent_lifecycle.py tests/test_agent_runtime.py tests/test_onprem_agent_chart.py` | 등록 시 UID 고정·마지막 접속·폐기 API 6건, 실행 루프·자격 증명 보관·in-cluster kubeconfig·401/403 구분 22건, Chart 렌더·스키마·권한·패키징 13건 |
| `alembic upgrade head` → `alembic check` → `downgrade 20261105_01` → `upgrade head` (SQLite) | 차이 없음, 왕복 성공 |
| `make openapi` | `/agent-admin/agents/{agent_id}`, `/agent-admin/agents/{agent_id}/revoke`, `EnrollmentRequest.cluster_uid` 선택 반영. 생성 결과 검토 |
| `docker build -f infra/onprem-agent/Dockerfile` (linux/arm64) | 90MB, UID 65532, kubectl v1.37.1·Helm v4.3.1 체크섬 검증, HTTP URL은 `SECURE_PLATFORM_URL_REQUIRED`로 종료 코드 2 |
| `scripts/smoke_onprem_agent_install.py` (k3d v5.9.0 / K3s v1.36.4+k3s1 / ARM64, 전용 kubeconfig, context `k3d-cmp-agent-install`, 플랫폼 API는 자체 서명 CA HTTPS) | [증거](../../docs/verification/provisioning-15-k3s.json): ① Chart 설치 → cluster UID 없이 발급한 토큰으로 등록, 등록 UID = kube-system UID, `cmp-agent-enrollment` 삭제·`cmp-agent-credential`만 남음 ② 승인 배포를 실제 Activity + 클러스터 안 Agent가 처리 → `SUCCEEDED READY` ③ 결과 유실 주입 + Agent Pod 삭제 → 새 Pod가 재등록 없이 검증만으로 복구, Helm revision 1 유지 ④ 플랫폼 중단 12초 → 1·2·4·8초 백오프 후 재접속, Pod 재시작 0 ⑤ 운영자 폐기 → `AGENT_UNAUTHORIZED`, 종료 코드 2 ⑥ Release·두 Namespace 삭제, 기본 kubeconfig 불변 |

TDD 메모: API·실행 루프·Chart는 실패 테스트를 먼저 작성했다. k3d 첫 실행에서 Pod 안 kubectl이 암묵적 in-cluster 설정을 쓰지 않고 `localhost:8080`으로 접속하는 문제와, 자격 증명 Secret 읽기 실패 시 Agent가 재시도 없이 종료되는 문제를 발견했다. 각각 실패 테스트를 추가한 뒤 (a) ServiceAccount 토큰 **파일**을 참조하는 kubeconfig를 생성해 kubectl·Helm에 명시하고, (b) 자격 증명 읽기·저장을 백오프로 감싸며 발급받은 자격 증명은 저장될 때까지 메모리에 유지하도록 고쳤다(RED → GREEN, 재실행 통과). 기존 결과 보고 API가 인증 실패를 409로 돌려주던 것을 403으로 바로잡았다(폐기 감지에 필요).

정리: `k3d cluster delete cmp-agent-install`로 클러스터와 Registry(`cmp-agent-reg`)를 함께 제거했고 `k3d cluster list`/`registry list`가 비어 있음을 확인했다. 기본 kubeconfig는 전후 SHA-256이 같다. 로컬 Docker에 `cmp-onprem-agent:dev`, `localhost:5511/cmp-probe:test`, `postgres:16-alpine` 이미지가 남아 있다. 검증 중 플랫폼 API를 `0.0.0.0`의 임의 포트로 잠시 열었다(Pod가 `host.k3d.internal`로 접근하기 위함, 무작위 관리자 키, 종료 후 닫음).

### 미검증·제한 사항

- 릴리스 워크플로(`agent-release.yml`)는 실행하지 않았다. 로컬에 actionlint/act가 없어 YAML 구문과, 워크플로가 호출하는 `scripts/package_agent_chart.py`(테스트 포함)만 확인했다. 첫 `agent-v*` 태그 push와 GHCR 패키지 공개는 외부 공개이므로 별도 승인 후 진행한다. 저장소 공개 전에는 패키지가 비공개라 인증 없이 받을 수 없다.
- amd64 이미지는 빌드하지 않았다(검증은 arm64). 워크플로가 두 아키텍처를 빌드한다.
- NAT 뒤 원격 노트북, 실제 공개 플랫폼 주소·공인 인증서, 다양한 K3s 배포판·macOS VM, 장시간 실행은 확인하지 않았다.
- ClusterRole에 클러스터 전체 Secret 읽기·쓰기가 포함된다(H2). 다른 Namespace 보호는 코드 검사(`NAMESPACE_NOT_OWNED`)에 의존한다.
- Agent가 실행할 ON_PREM Plan을 운영 코드가 게시하는 경로는 없다(18). 검증은 테스트 지원 코드로 Plan을 게시했다.
- `spec.md` 요구사항·수용 기준은 바뀌지 않았다. 04A 등록 계약 변경(H3)과 폐기·마지막 접속은 `onprem-shared-deployment.md`·`shared-contracts.md`·OpenAPI에 반영했다.
