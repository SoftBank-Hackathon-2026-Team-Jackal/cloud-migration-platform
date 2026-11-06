# Ticket 52 — Agent 작업 시한·취소·정리 작업과 운영 보강

## 상태
IN_PROGRESS (로컬 구현·테스트·k3d 실측 완료, 리뷰 대기) — [Issue #187](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/187). 우선순위 중간(P1/P2), 담당 민서. 15·17·51 후속, #164(40) 참고. **새 Agent 릴리스가 필요하다(Agent 코드·Chart·RBAC 변경, 태그는 사용자 승인 뒤).**

## 배경 (2026-11-06 실동작 점검 C·B, 이전 N9·N13·N14)
- **C-04 / N14 (P1, 실재현):** Agent가 꺼진 동안 승인 실행 Activity는 30분 뒤 `UNKNOWN AGENT_OFFLINE`을 반환하지만 작업은 `QUEUED`로 남는다. Agent가 돌아오면 그 작업을 받아 운영 배포를 한다. Workflow는 이미 끝났으므로 추적되지 않는 배포가 생긴다. 작업 취소 API도 없다.
- **C-05 / N9 ON_PREM (P1):** 배포를 지우는 작업 유형이 없다. 사용자 K3s에 데이터가 든 PVC·Release·Secret이 남고, 다른 이전을 같은 Namespace로 하면 이전 Release와 섞이거나 Secret 이름 충돌(`SECRET_NAME_CONFLICT`)이 난다.
- **C-06 일부 / N13 (P1):** `networkPreflight.image` 기본값이 없어 기본 설치 Agent는 NETWORK_PREFLIGHT를 항상 `PREFLIGHT_NOT_CONFIGURED`로 거절한다.
- **P2:** 프록시 설정 불가(C-08), 폐기된 Agent Pod 재시작 반복(C-09), 자격 분실 시 새 Agent ID·새 Plan 필요(C-11), 작업을 하나씩만 처리(C-12), ClusterRole이 클러스터 전체 Secret·exec·Pod 삭제 보유(B-17, 51이 `pods` delete를 무조건 추가).
- **재검사 신규 (P2):** C-13 실패 Revision 동안 DB 중단, C-14 51 복원 뒤 ECR pull Secret 갱신 중단(코드 근거), C-15 앱만 실패하면 DB는 새 Revision으로 남음.

## 관련 요구사항
PROV-009, PROV-015, PROV-016, PROV-017, PROV-021

## 선행 작업
12, 15, 17, 29, 51(DONE·병합). #164(Agent 버전·지원 작업 유형 보고)는 선행이 아니라 관계만 기록한다(D11).

## 범위
- 포함: 작업 시한(플랫폼 claim·Activity·Agent 양쪽), 운영자 취소 API와 `EXPIRED`·`CANCELLED` 상태, 정리 작업 유형 `ENVIRONMENT_TEARDOWN`과 생성 API, PVC·Namespace 정책, probe 이미지 기본값, Chart 프록시 값, 폐기 시 정지 유지, 정리 작업 우선 claim, ClusterRole 축소(`watch` 제거)·정리 권한, C-14 수정, C-11·C-13·C-15·`pending-*` 결정 기록, 지원하지 않는 작업 유형 응답 정의
- 제외: 자격 재발급 API 구현(C-11, 설계만 D15), Namespace 한정 Role 모드(B-17, 설계만 D16), 노드 아키텍처 사전 대조(#155, C-03), Agent 버전 보고·dispatch 사전 차단(#164), 원본 DB 인증 Secret 전달(C-06 데이터 담당), UDP 대체 터널(C-10), 정리 시점을 정하는 Workflow 단계(Traffic·데이터 담당과 순서 합의 필요)

## 수용 기준 (Issue #187)
- [x] 시한 지난 작업은 실행되지 않고 결과가 보고된다. — Agent를 끈 채 Activity가 대기 한도에 닿자 `FAILED AGENT_OFFLINE`, 작업 `EXPIRED`. 돌아온 Agent는 받은 작업이 없고 Helm 이력이 그대로다(k3d 3단계). claim 시점·Agent 쪽 시한은 단위 테스트.
- [x] 정리 작업으로 대상 Namespace 리소스가 지워진다(k3d 확인). — 데이터 유지: Release·관리 Secret·관리 NetworkPolicy·pull Secret 삭제, PVC·DB 비밀번호·사용자 Secret·사용자 정책·Namespace 유지(0.7초). 데이터 삭제: PVC·Namespace까지 삭제(52초).
- [x] 값 없이 설치해도 preflight가 기본 이미지로 돈다. — `networkPreflight` 값 없이 이 브랜치 Agent 이미지를 설치해 PASS(기본 Digest 사용).

## Test Seam
- 플랫폼 작업 수명: `cmp_api.agent_registry.poll_job`·`report_job`, 새 `cmp_api.agent_jobs`(취소·만료·정리 작업 생성), 관리자 HTTP 경로(`TestClient`): `tests/test_agent_job_lifecycle.py`
- 승인 실행 Activity: `execute_on_prem_plan`(ActivityEnvironment, 실제 claim/report 서비스): `tests/test_onprem_rollout.py`
- Agent: `RolloutAgent.poll_once`·`RolloutRunner.rollout`·새 `TeardownRunner.run` + 12의 `FakeCluster`: `tests/test_agent_teardown.py`, `tests/test_onprem_agent.py`, `tests/test_image_pull.py`
- 폐기 정지: `AgentDaemon.run`·`run_tunnel`(가짜 대기): `tests/test_agent_runtime.py`, `tests/test_agent_tunnel.py`
- Chart: 실제 `helm template`: `tests/test_onprem_agent_chart.py`
- 실제 K3s·Helm·RBAC: `scripts/smoke_onprem_job_lifecycle.py`(격리 k3d, Agent ServiceAccount 토큰 kubeconfig), `scripts/smoke_onprem_preflight.py`(Chart 기본 probe, 폐기 관찰)
- 놓치는 것: 실제 프록시 환경(C-08), 실제 Agent 이미지(릴리스 전), 12시간 ECR 토큰 만료 실측(C-14), Helm 3

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 시한 저장 | `agent_jobs`에 `deadline_at`(epoch 초, 선택)·`cancel_requested_at`(선택) 열을 더한다(Alembic `20261106_02`). 작업 본문(payload)은 바꾸지 않는다 | 본문은 `extra=forbid`라 필드를 더하면 구버전 Agent가 `UNSUPPORTED_JOB`으로 거절하고, 같은 Revision 재dispatch의 동일 본문 비교(`AGENT_JOB_CONFLICT`)도 깨진다 |
| D2 시한 값 | 운영 배포 = dispatch 시각 + Activity 대기 한도(기본 1800초). 정리 = 생성 + 3600초. NETWORK_PREFLIGHT는 시한 없음 | 승인 실행이 결과를 기다리는 동안만 시작할 수 있어야 한다. Preflight는 읽기 전용이고 자체 재큐(`REUSE_SECONDS`) 수명이 있어 범위 밖 |
| D3 플랫폼 강제 | claim 때 `QUEUED`이고 시한이 지났으면 Agent에 주지 않고 `EXPIRED`(`JOB_EXPIRED`). `RUNNING` 재수신 때 시한이 지났으면 `UNKNOWN JOB_EXPIRED`(부분 적용 가능, 재실행 안 함). Activity가 대기 한도에 닿았을 때 아직 `QUEUED`면 그 자리에서 `EXPIRED`(`AGENT_OFFLINE`)로 닫고 `FAILED AGENT_OFFLINE`을 반환한다(이전 `UNKNOWN`). `RUNNING`이면 기존대로 `UNKNOWN AGENT_RESULT_PENDING` | 상태 전환은 `QUEUED` 조건부 갱신이라 claim과 경쟁해도 둘 중 하나만 된다. 실행되지 않았음이 확정이므로 `FAILED`(새 Revision으로 재시도). 같은 Revision 재실행은 14 정책상 경로가 아니다 |
| D4 Agent 강제 | claim 응답에 `expires_in_seconds`(작업 본문 밖, 남은 초)를 싣는다. Agent는 받은 순간의 단조 시계에 더해 시한을 잡고, **첫 클러스터 변경 직전**(읽기 검사·다운로드 뒤)에 지났으면 `FAILED JOB_EXPIRED`로 변경 없이 보고한다 | 남은 초를 쓰면 플랫폼·노드 시계 차이와 무관하다. 구버전 Agent는 모르는 필드를 무시하고(D3만 적용) 동작한다. Helm 단계 중간에는 끊지 않는다(`pending-*` 위험) |
| D5 취소 | `POST /agent-admin/jobs/{job_id}/cancel`(관리자 키). `QUEUED` → `CANCELLED`(`JOB_CANCELLED`, 아무것도 실행 안 됨). `RUNNING` → `cancel_requested_at` 기록: 산출물·env-secrets·image-pull 조회를 409 `JOB_CANCELLED`로 막아 변경 전이면 멈추고, 재수신은 `UNKNOWN JOB_CANCELLED`. 이미 Helm 단계에 들어갔으면 끝까지 수행하고 보고를 그대로 기록한다. 끝난 작업은 409 `JOB_ALREADY_FINISHED`. 대상은 운영 배포·정리 작업만(Preflight는 409 `CANCEL_NOT_SUPPORTED`) | 운영자 조치다(승인자 UI는 후속). Helm 실행 중 강제 중단은 51이 다루는 `pending-*` 고착을 만든다 |
| D6 결과 분류 | `EXPIRED`·`CANCELLED`는 실행 전 확정 실패: Activity `FAILED`, `JOB_EXPIRED`·`JOB_CANCELLED`·`AGENT_OFFLINE`을 `PRECONDITION_CODES`에 추가(`RETRY_AFTER_FIX`). 상태 조회 `state`에 `EXPIRED`·`CANCELLED` 추가(OpenAPI 갱신) | 대상 변경이 없었음을 결과에 정확히 남긴다 |
| D7 정리 작업 | 새 작업 유형 `ENVIRONMENT_TEARDOWN`(`cmp_contracts.onprem_teardown.TeardownJob`). 대상은 **기존 운영 배포 작업에서만 파생**한다(Namespace·Release·environment_id·DB 비밀번호 Secret 이름); 요청이 임의 Namespace를 고를 수 없다. 생성 경로: `POST /agent-admin/jobs/{rollout_job_id}/teardown` `{delete_data}`(관리자 키) → `cmp_api.agent_jobs.create_teardown`. `job_id`는 (원 작업, delete_data)에서 결정된다. 끝난 실패 작업은 같은 ID로 다시 대기열에 넣고, 성공한 작업은 그대로 돌려준다 | 정리를 언제 할지(Traffic 롤백·데이터 이전 후)는 Workflow 순서가 아직 Traffic·데이터 담당과 합의되지 않았다. Activity는 그 합의 뒤 같은 함수를 부르면 된다. 정책상 데이터가 있는 자원을 자동 삭제하지 않으므로 지금은 운영자 경로만 둔다 |
| D8 PVC·Namespace 정책 | 기본 `delete_data=false`: 앱→DB Release `helm uninstall`, Namespace의 관리 Secret(`cmp.dev/managed=true`, **DB 비밀번호 Secret 제외**)·관리 NetworkPolicy·ECR pull Secret 삭제. PVC·DB 비밀번호 Secret·Namespace(소유 주석)는 남겨 같은 environment로 다시 배포하면 데이터를 이어 쓴다. `delete_data=true`: 위에 더해 DB PVC와 Namespace 자체를 지운다(소유 주석이 맞을 때만, 사용자가 넣은 Secret 포함 전부) | DB Chart PVC는 `helm.sh/resource-policy: keep`이라 uninstall로 지워지지 않는다. 비밀번호 Secret을 지우면 남긴 PVC의 DB와 새 비밀번호가 어긋난다. 데이터 삭제는 되돌릴 수 없어 명시 요청에서만 한다 |
| D9 정리 실행 규칙 | Agent: kube-system UID → Namespace 소유(`cmp.dev/environment-id` 일치, 없으면 `FAILED NAMESPACE_NOT_OWNED`; Namespace가 없으면 할 일이 없어 `SUCCEEDED TORN_DOWN`) → 시한 확인(D4) → Release 상태와 무관하게 `helm uninstall --ignore-not-found`(`pending-*`·`uninstalling`·`failed` 포함) → Secret·NetworkPolicy(·PVC·Namespace) 삭제. Namespace 삭제는 `--helm-timeout` 동안 사라지기를 기다린다. 결과: `SUCCEEDED TORN_DOWN`, `FAILED CLUSTER_MISMATCH/NAMESPACE_NOT_OWNED/JOB_EXPIRED`, `UNKNOWN TEARDOWN_INCOMPLETE`(어떤 삭제가 실패)·`NAMESPACE_DELETE_PENDING`·`KUBERNETES_UNAVAILABLE` | 정리는 "남은 것을 지우는" 작업이라 재실행이 안전하다(멱등). 소유 확인은 배포와 같은 기준이다 |
| D10 순서·동시성 (C-12) | Agent는 한 번에 하나를 유지한다. claim은 대기 중인 **정리 작업을 먼저** 준다. 같은 Namespace에 진행 중인 운영 배포가 있으면 정리 생성은 409 `ROLLOUT_IN_PROGRESS`(먼저 취소), 정리가 진행 중이면 운영 배포 dispatch는 `TEARDOWN_IN_PROGRESS`. Preflight 대기 시한은 롤아웃 최대 시간(Helm 2단계 × 시한 × 2 + 검증)보다 길게 잡을 것을 데이터 담당에 권고한다 | 별도 스레드 처리는 kubectl·Helm 동시 실행과 같은 Namespace 경합을 새로 만든다. "정리가 막히지 않게"는 우선 claim과 상호 배제로 충분하다. 동시 처리는 후속 |
| D11 지원하지 않는 작업 유형 (#164) | 구버전 Agent(≤ `agent-v0.3.0`)는 정리 본문을 운영 배포로 검증하다 실패해 **아무것도 바꾸지 않고** `FAILED UNSUPPORTED_JOB`을 보고한다. 새 Agent도 모르는 `mode`에는 같은 응답을 정의한다. 플랫폼은 그 결과를 관리자 조회(`GET /agent-admin/jobs/{job_id}`)에 그대로 보인다. 사전 차단(작업 생성 전 `AGENT_UPGRADE_REQUIRED`)은 #164가 Agent 버전·지원 유형을 보고하면 그 정보로 한다 | 지금 Agent는 버전을 보고하지 않는다. 실패가 "변경 없음"으로 확정되므로 안전하다 |
| D12 probe 이미지 기본값 (N13) | `networkPreflight.image` 기본 = `docker.io/library/postgres@sha256:0cefaabd…5731ab`(공식 postgres 16.15-alpine3.24 multi-arch index, amd64·arm64, MNP-01 k3d에서 PASS 확인한 그 Digest). 기본 설치에 Preflight 권한(Pod create/delete, `pods/log`)이 함께 생긴다. 끄려면 `--set networkPreflight.image=""` | 공개 이미지라 추가 자격이 없고, Digest 고정이라 태그 이동에 흔들리지 않는다. Agent 이미지에 넣으면 이미지가 커지고 GHCR 비공개 문제(C-01)를 공유한다. Docker Hub 제한·에어갭 환경은 미러 Digest로 덮어쓴다 |
| D13 프록시 (C-08) | `proxy.https`(·`proxy.http`)·`proxy.noProxy` 값. 설정하면 두 컨테이너에 `HTTPS_PROXY`/`https_proxy`(`HTTP_PROXY`/`http_proxy`)·`NO_PROXY`/`no_proxy`를 넣는다. `noProxy` 기본 `localhost,127.0.0.1,.svc,.cluster.local,10.42.0.0/16,10.43.0.0/16`(K3s 기본 Pod·Service CIDR) | Python urllib(플랫폼 호출)은 환경 프록시를 따르고, kubectl(Go)은 CIDR `NO_PROXY`를 지원해 클러스터 API(`10.43.0.1`)를 제외한다. WireGuard(UDP)는 HTTP 프록시를 지나지 않는다(C-10) |
| D14 폐기 시 정지 (C-09) | `AGENT_UNAUTHORIZED`·`ENROLLMENT_REJECTED`면 프로세스를 끝내지 않고 `stopped` 로그 한 번 뒤 SIGTERM까지 대기한다(플랫폼 요청 0). `tunnel`은 인터페이스를 내린 뒤 같은 방식으로 대기한다. 종료 코드 2는 SIGTERM 뒤에만 | 재시작 반복은 실패 Pod로 보이고 매번 403 요청을 보낸다. 폐기는 되돌릴 수 없으므로(ID 재사용 금지) 다시 확인할 이유가 없다. Pod는 `Running`으로 보이므로 문서에 로그 확인과 `helm uninstall`을 안내한다 |
| D15 자격 재발급 (C-11) | **설계만, 후속.** `POST /agent-admin/agents/{id}/reissue` → 같은 `agent_id`·등록 `cluster_uid`에만 쓸 수 있는 일회용 재등록 토큰. 사용하면 `credential_digest`를 교체해 이전 자격을 즉시 무효로 한다. Plan target의 `agent_id`가 그대로라 진행 중 Migration은 Plan을 다시 게시하지 않는다 | 등록 테이블·토큰 수명(N15)·Agent 재등록 흐름을 함께 바꿔야 해 범위가 크다 |
| D16 권한 (B-17) | ClusterRole에서 `watch` 동사를 모두 뺀다(51 이후 Helm `--wait` 없음, Agent는 `get`/`list`만 쓴다). 정리를 위해 `namespaces` delete를 더한다. `pods` delete(51)는 클러스터 범위로 둔다. Namespace 한정 Role 모드(`rbac.namespaces` 사전 선언)는 설계만 후속 | RBAC는 라벨로 범위를 좁힐 수 없다. Agent가 실행 중 Namespace마다 Role을 만들려면 자기가 가진 권한 이상을 줄 수 없어(권한 상승 방지) 결국 같은 클러스터 권한이나 `escalate`·`bind`가 필요해 더 위험하다. 코드 쪽 소유 확인(주석)이 실제 경계다. k3d에서 Agent ServiceAccount 토큰으로 배포·정리를 실측해 축소한 권한을 확인한다 |
| D17 C-14 | Agent: 실패로 끝난 운영 배포(READY 아님)는 pull Secret의 `cmp.dev/job-id`를 이 작업 전 값으로 되돌린다. 플랫폼: `job_image_pull`은 `SUCCEEDED READY` 작업이 최신 Revision이 아니어도 **자기 승인 기록이 유효하고 같은 Plan의 더 새 Revision 작업 중 READY가 없으면** 허용한다(= 실제로 배포돼 있는 최신 성공 Revision) | 51 복원 뒤 클러스터에서 도는 것은 이전 성공 Revision이다. 그 작업의 토큰 갱신을 막으면 12시간 뒤 재스케줄 때 `ImagePullBackOff`가 된다 |
| D18 C-13 | 기록만. 실패 Revision 동안 DB 중단은 단일 replica·RWO StatefulSet의 성질이다(51 R7). 줄이는 방향: Plan Preview에 "DB 재시작이 일어나는 변경(이미지·리소스·affinity)" 표시와 별도 확인, #155 노드 아키텍처 사전 대조로 정착하지 못할 Revision을 게시 전에 막기 | 둘 다 Planning 범위(#155, 18) 변경이라 이 티켓에서 하지 않는다 |
| D19 C-15 | 정책: 앱만 실패하면 DB는 새 Revision으로 둔다(51 R3 유지). 앱 Release만 이전으로 되돌린다 | 정상 정착한 DB를 다시 재시작하면 중단이 한 번 더 생긴다. DB Values 변경은 리소스·이미지 패치 수준이며 PostgreSQL 메이저 버전 변경은 PVC 데이터와 호환되지 않아 어차피 별도 이전이 필요하다. 호환되지 않는 DB 변경 경고는 D18의 Preview 표시와 함께 후속 |
| D20 `pending-*`·`uninstalling` (51에서 넘김) | 운영 배포는 지금처럼 `UNKNOWN INSPECTION_REQUIRED`로 손대지 않는다. 운영자 해법은 설치 문서 §6 수동 rollback 또는 정리 작업(D9는 상태와 무관하게 uninstall) 뒤 새 Revision | Helm 잠금은 사람·다른 도구가 잡고 있을 수도 있어 Agent가 "버려진 잠금"을 확정할 수 없다 |

## 구현 위치
- 계약: `packages/contracts/src/cmp_contracts/onprem_teardown.py`(신규), `onprem_rollout.py`(`RolloutState`)
- 플랫폼: `apps/api/src/cmp_api/agent_registry.py`(열·claim 시한·우선순위), `agent_jobs.py`(신규: 취소·만료·정리 생성·관리자 조회), `onprem_rollout.py`(dispatch 시한·상호 배제·outcome·C-14), `routes/agent_jobs.py`(신규 관리자 경로), `routes/agents.py`(claim 응답·본문 union·취소 조회 차단), `routes/onprem_rollout.py`(state), `migrations/versions/20261106_02_agent_job_deadline.py`
- Worker: `apps/worker/src/cmp_worker/activities/onprem_execution.py`(대기 한도에서 만료)
- Agent: `packages/providers/src/cmp_providers/agent_teardown.py`(신규), `onprem_agent.py`(시한·정리 분기·C-14), `agent_runtime.py`·`agent_tunnel.py`(폐기 정지), `provisioning_result.py`(D6)
- Chart: `infra/helm/onprem-agent/values.yaml`·`values.schema.json`·`templates/deployment.yaml`·`templates/rbac.yaml`·`README.md`
- 실측: `scripts/smoke_onprem_job_lifecycle.py`(신규, Agent ServiceAccount 토큰), `scripts/smoke_onprem_preflight.py`(`--probe-image` 선택, `--observe-revocation`)

## 검증
- 단위: `tests/test_agent_job_lifecycle.py`(15), `tests/test_agent_teardown.py`(13), 바뀐 `test_onprem_rollout`·`test_plan_publish`·`test_image_pull`·`test_agent_runtime`·`test_agent_tunnel`·`test_onprem_agent_chart` 통과
- RED: 새 테스트가 구현 전 실패(모듈 없음, Chart 스키마 `proxy` 거절·`watch` 존재·probe 기본값 없음, 폐기 시 즉시 종료, 29의 갱신 거절)
- `make check`: 2041 passed, 9 skipped (ruff lint·format 포함)
- `make openapi`: 관리자 경로 3개·`expires_in_seconds`·`EXPIRED`/`CANCELLED` 추가(삭제 없음)
- Alembic(SQLite): `upgrade head` → `check`(변경 없음) → `downgrade 20261106_01` → `upgrade head`, head `20261106_02`
- `make smoke-temporal`: 로컬 Temporal이 떠 있지 않아 실행하지 않았다. Workflow 코드는 바뀌지 않았고 승인 Workflow 경로는 `test_approval_workflow_dispatches_on_prem_and_records_ready`(Temporal 테스트 환경)로 확인했다

### k3d 실측 (2026-11-06)
k3d v5.9.0 / K3s v1.36.4+k3s1 / arm64, Helm v4.3.1, 전용 kubeconfig, context `k3d-cmp-p52`. 가짜 플랫폼 ECR 호스트(`123456789012.dkr.ecr.ap-northeast-2.amazonaws.com`)를 노드 containerd가 로컬 레지스트리로 미러(점검 C 방식, AWS 호출 없음, 가짜 토큰).

**① 수명·정리·C-14** — `scripts/smoke_onprem_job_lifecycle.py`. Agent는 호스트에서 돌지만 **Chart가 만든 ServiceAccount 토큰**으로 클러스터에 접근한다(이 브랜치 ClusterRole: `watch secrets` no, `delete namespaces` yes). [증거](../../docs/verification/provisioning-52-k3s.json)

| 단계 | 결과 |
| --- | --- |
| 1 rev1 | `SUCCEEDED READY`, pull Secret 소유 작업 = rev1 |
| 2 C-14: 노드와 다른 아키텍처 rev2 | `FAILED WORKLOAD_NOT_READY`, DB Release 복원(`superseded, superseded, deployed`), DB Pod Ready. pull Secret 소유 작업이 rev1로 돌아왔고 갱신 시점을 강제하자 `REFRESHED`(플랫폼이 rev1 작업 토큰 발급) |
| 3 시한 | Agent 끈 채 rev3 → Activity `FAILED AGENT_OFFLINE`, 작업 `EXPIRED`. 돌아온 Agent poll 결과 없음, Helm 이력 불변 |
| 4 취소 | 대기 중 rev4 → 관리자 취소 `CANCELLED JOB_CANCELLED`, Agent poll 결과 없음 |
| 5 정리(데이터 유지) | `SUCCEEDED TORN_DOWN`. 전: Release 2, Secret `cmp-ecr-pull`·`demo-app-env`·`demo-db-auth`, 정책 `cmp-managed-seed`·`user-policy`. 후: Release 0, Workload 0, Secret `demo-app-env`·`demo-db-auth`, 정책 `user-policy`, PVC `demo-db`, Namespace 유지 |
| 6 정리(데이터 삭제) | `SUCCEEDED TORN_DOWN`, Namespace·PVC·Secret 모두 없음(52초) |
| 정리 | 두 Namespace 삭제, ClusterRole/Binding 삭제, 기본 kubeconfig 불변, 가짜 토큰이 증거에 없음 |

주: 이 Plan 픽스처에는 26 NetworkPolicy 산출물이 없어, 관리 라벨 정책 하나와 사용자 정책 하나를 직접 넣어 선택 삭제를 확인했다. 2단계 직후 앱은 DB 재시작 뒤 재연결 전이라 0/1이었다(51과 같은 동작).

**② 기본 probe 이미지·폐기 정지** — `scripts/smoke_onprem_preflight.py`를 `--probe-image` 없이(Chart 기본값) 실행. 이 브랜치로 빌드한 Agent 이미지(`cmp-onprem-agent:p52`, `k3d image import`)를 Chart로 설치, in-cluster ServiceAccount. [증거](../../docs/verification/provisioning-52-preflight-k3s.json)

| 시나리오 | 결과 |
| --- | --- |
| 정상 | `PASS`(probe 이미지 = Chart 기본 Digest) |
| 같은 요청 | `PASS`(재사용) |
| 틀린 비밀번호 | `FAIL`(MNP-01과 같음) |
| 소유하지 않은 Namespace | `UNKNOWN NAMESPACE_NOT_OWNED`, Pod 생성 없음 |
| 폐기 후 90초(C-09) | Pod `Running`, `agent` 재시작 0회, `stopped AGENT_UNAUTHORIZED` 한 번. 이어서 `helm uninstall` 정상 |

같은 스크립트의 첫 재실행은 Helm 4 `--wait`가 Agent가 등록 직후 지우는 enrollment Secret을 기다리다 실패했다(스크립트 경합). 스크립트에서 `--wait`를 빼고 등록 대기만 남겼다. 사용자 설치 명령에는 원래 `--wait`가 없다.

정리: `k3d cluster delete cmp-p52`, `k3d registry delete k3d-cmp-p52-reg`, 로컬 이미지 삭제. `k3d cluster list`·`registry list` 비어 있음.

## 미검증
- 실제 HTTPS 프록시 환경(C-08): Chart 렌더만 확인
- 실제 ECR 12시간 토큰 만료 뒤 재스케줄(C-14): 갱신 허용·소유 작업 복원까지만 확인
- `tunnel` 컨테이너의 폐기 정지(smoke는 터널을 끔): 단위 테스트만
- Agent가 Helm 단계 실행 중일 때의 `RUNNING` 취소 요청(D5): 단위 테스트만
- 구버전 Agent(v0.3.0)가 정리 작업을 `UNSUPPORTED_JOB`으로 거절하는 실측: 같은 검증 경로(`ProductionRolloutJob` 검증 실패)를 단위 테스트로만 확인
- 게시된 GHCR 이미지(새 릴리스 승인 대기), Helm 3

## 문서 변경
- `specs/provisioning/onprem-agent-install.md` §2·§3·§4·§5·§6
- `specs/provisioning/onprem-shared-deployment.md` Agent 등록·승인된 운영 배포
- `specs/provisioning/shared-contracts.md`(작업 상태·정리 작업)
- `specs/provisioning/plan.md` §12 표
- `infra/helm/onprem-agent/README.md`
- 티켓 12·15·17·25·29·42·51에 52 후속 표시
- OpenAPI 재생성

## 완료 체크리스트
- [x] 티켓·tasks.md 행·Issue 연결
- [x] RED → GREEN 단위 테스트
- [x] k3d 실측(시한 거절·정리·기본 probe·복원 뒤 pull Secret·폐기 정지)
- [x] SDD 문서 갱신
- [ ] 리뷰·병합(오케스트레이터)
- [ ] Agent 릴리스(사용자 승인)
