# Ticket 12 — 승인된 K3s Agent 운영 배포

## 상태
IN_PROGRESS — [Issue #36](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/36). 2026-11-04 착수 결정 E1~E9와 Test Seam 검토 완료(아래).

## 목적
승인된 K3s 계획을 Agent에 보내고 기존 클러스터에서 DB·앱 준비를 확인한다.

## 관련 요구사항
PROV-009, PROV-015, PROV-016, PROV-017, PROV-018

## 선행 작업
Blocked by: 04A, 07

## 작업 범위
- 포함: Agent ID·작업 ID·Plan Digest 검사, Namespace·StorageClass·Helm 상태 재조회, 운영 배포 검증을 구현한다.
- 04A의 소유자·Agent 등록과 [공통 렌더 입력/출력](../../specs/provisioning/onprem-shared-deployment.md)을 사용한다. 원격 등록·차트 변환을 복제하지 않는다.
- 제외: 온프레미스 외부 중계·Envoy·WireGuard 생명주기

## 수용 기준
- [ ] 승인되지 않거나 변경된 Plan의 Agent 배포 요청을 거부한다.
- [ ] Agent가 오프라인이면 작업 상태를 보존하고 재연결 후 조회한다.
- [ ] PVC·DB·앱 상태와 내부 Endpoint를 확인하고 실패 시 `READY` 금지다.

## 테스트 전략
- 제안 Test Seam: 승인 Workflow → Agent 작업/실제 K3s 상태. 외부 트래픽 경로는 확인하지 못한다.
- 실행: 가짜 Agent의 중복/오프라인 계약 테스트와 격리 K3s 배포 검증; `make check`.

## 완료 시 확인할 동작
승인된 Plan에 연결된 K3s 운영 배포 상태를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Agent 작업 통지·인증, K3s 사전 준비와 외부 접근 인계 경계를 정한다.
- 구현 중 변경: 2026-11-04 착수 결정 E1~E9 참조(실행 연결은 08 소유 dispatch 계약, 작업 본문·산출물 전달·오프라인 보존·READY 기준).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 착수 결정 (2026-11-04)

사용자 지시: 기획 문서의 최신 결정을 우선하고, 문서에 없는 항목은 구현자가 최선안을 선택해 기록한다. 사용자는 이 작업에 한해 병렬 세션을 허용했다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| E1 실행 연결 (**dispatch owned by 08**) | 07의 단일 `execute_approved_plan`은 08이 claim → provider Activity → complete로 나눈다. `claim_approved_execution`(DB 재확인 + RUNNING 표식)과 `complete_approved_execution`, `ProvisioningApprovalWorkflow` 변경은 08 소유다. 12는 `PROVISIONING_QUEUE`의 Activity `execute_on_prem_plan(request) -> {status: SUCCEEDED/FAILED/UNKNOWN, code, resource_refs}`만 새 모듈 `cmp_worker.activities.onprem_execution`에 구현하고 `workers/provisioning.py`에 등록 1줄을 추가한다. 입력은 claim 결과 필드(`approval_id, plan_id, revision, provider, scope, manifest_digest` 등)다. 08 병합 전에는 `ActivityEnvironment`로 직접 검증한다. | 08 세션 조정 요청(2026-11-04). Agent 작업은 5분 Activity보다 길다. 07의 Workflow/claim 경로를 12가 재구성하지 않아 충돌을 피한다. |
| E2 Agent 작업 본문 | `cmp_contracts.onprem_rollout.ProductionRolloutJob`(mode `PRODUCTION_ROLLOUT`): `job_id`, `approval_id`, `plan_id`, `revision`, `manifest_digest`, `environment_id`, `agent_id`, `cluster_uid`, `namespace`, `storage_class`, DB(release, Chart·Values ArtifactRef, 비밀번호 Secret 이름·키), App(release, Chart·Values ArtifactRef, port, health_path, 필요한 사용자 Secret 이름·키 목록). `job_id = rollout-` + SHA-256(`plan_id:revision`) 앞 24자로 Revision당 하나다. 값은 **업무 DB의 Plan Revision과 Digest 검증을 통과한 Values 바이트에서만** 만든다. Secret 원문·Values/Manifest 본문은 싣지 않는다. 허용 동작은 두 Release의 Helm install/upgrade와 읽기 검사뿐이다. | plan.md §7("Plan ID·Revision·작업 ID·산출물 Digest·Manifest/Values 참조와 허용 동작만"). 04의 `SandboxJob`은 테스트 Namespace 전용이라 재사용하지 않고 별도 모드로 둔다. |
| E3 산출물 전달 | 승인 Plan의 `database_chart/values`, `application_chart/values` ArtifactRef를 사용한다. Chart 산출물 = `helm package` tgz 바이트, Values = 04B v2 렌더의 `PostgresChartValues`/`ApplicationValuesV2`를 `cmp-json-v1`로 직렬화한 바이트. 플랫폼 저장소는 `ArtifactStore` 경계(P0 구현: Digest 파일명 로컬 디렉터리, S3는 후속). Agent는 인증된 `GET /agents/{agent_id}/jobs/{job_id}/artifacts/{sha256}`로 **자기 작업에 포함된 참조만** 받고 크기·Digest를 검증한 뒤 사용한다. | 04A 전송은 참조만 있어 바이트 경로가 필요했다. 본문을 작업 JSON에 넣으면 큰 Chart·이진 데이터가 업무 DB/History에 남는다. |
| E4 대상 고정 | Plan `target.agent_id`는 등록된 Agent여야 하고 `cluster_ref.id`는 그 Agent의 등록 `cluster_uid`(kube-system Namespace UID)와 같아야 한다. 작업 소유자는 Agent 소유자다. Values의 Namespace·StorageClass는 Plan의 Namespace·PVC StorageClass와 일치해야 한다. Agent는 변경 전 kube-system UID를 재확인한다. Namespace가 없으면 `cmp.dev/managed=true` 라벨·`cmp.dev/environment-id` 주석으로 만들고, 있으면 같은 주석이 있을 때만 사용한다(아니면 `NAMESPACE_NOT_OWNED`). | PROV-009 "Namespace를 준비". 사용자 K3s의 기존 Namespace를 덮어쓰지 않는 보수적 기본값. |
| E5 승인 재확인(이중) | Activity는 실행 직전 업무 DB에서 Plan Revision을 다시 읽어 ON_PREM·최신 Revision·manifest Digest 일치를 확인한다. 추가로 플랫폼의 Agent claim·산출물 다운로드 시점에도 `PRODUCTION_ROLLOUT` 작업은 해당 Revision의 APPROVE 기록·최신 Revision·Digest를 확인하고, 불일치면 Agent에 주지 않고 작업을 `FAILED`(`PLAN_NOT_APPROVED`/`STALE_REVISION`/`APPROVAL_TARGET_MISMATCH`)로 닫는다. | 수용 기준 1. 대기 중 새 Revision이 게시되면 오래된 작업이 실행되지 않게 한다. |
| E6 오프라인·재연결 | 업무 DB `agent_jobs` 행이 상태의 원천이다. Activity는 같은 `job_id`가 있으면 payload가 같을 때 재사용(다르면 `FAILED AGENT_JOB_CONFLICT`)하고 없을 때만 enqueue한다. 이후 heartbeat하며 상태를 주기 조회하고, 대기 한도(기본 30분, 설정 가능) 초과 시 작업을 취소하지 않고 `UNKNOWN`(`AGENT_OFFLINE` = QUEUED, `AGENT_RESULT_PENDING` = RUNNING)을 반환한다. Agent 재접속 시 claim은 자기 RUNNING 작업을 다시 준다(04A). Agent는 변경 전에 Helm Release를 조회한다: 둘 다 `deployed`면 재설치 없이 검증만, `pending-*`/`failed`면 `UNKNOWN INSPECTION_REQUIRED`(자동 재적용 금지), 없으면 설치. 상태 조회는 승인자 인증 `GET /provisioning/plans/{plan_id}/revisions/{revision}/rollout`. | plan.md §7/§9 "UNKNOWN/WAITING_AGENT로 보존하고 재연결 뒤 조회", "Heartbeat timeout만 보고 재발행하지 않음". |
| E7 Secret | DB 비밀번호 Secret이 없으면 Agent가 대상 Namespace 안에서 임의 값으로 만든다(값은 클러스터 밖으로 나가지 않음). 앱이 참조하는 사용자 Secret 이름·키는 사전에 있어야 하며 없으면 Helm 변경 전에 `FAILED SECRET_REFERENCE_MISSING`. | 04A/spool 정책: 작업·DB에 Secret 원문 금지. 사용자 Secret 업로드 경로는 후속. |
| E8 READY 기준 | Agent는 다음이 모두 통과할 때만 `SUCCEEDED` + `READY`를 보고한다: 클러스터 UID, Namespace 소유, StorageClass 존재, 두 Helm Release `deployed`, PVC `Bound`(지정 StorageClass), StatefulSet/Deployment의 observedGeneration·ready·updated = desired, 두 Service의 Ready Endpoint, DB Pod 내부 `pg_isready`, Service 프록시 경유 앱 health_path HTTP 2xx. 실패는 `FAILED` + 검사 코드(예: `PVC_NOT_BOUND`, `WORKLOAD_NOT_READY`, `DATABASE_NOT_READY`, `APPLICATION_HEALTH_FAILED`), 판단 불가는 `UNKNOWN INSPECTION_REQUIRED`. `resource_refs`는 Namespace, Release, 내부 Endpoint(`<db>.<ns>.svc:5432`, `<app>.<ns>.svc:<port>`), DB Secret 이름·키, job_id다. 외부 Ingress·트래픽·기능/데이터 테스트는 제외(13·Migration). | PROV-018 중 대상 내부에서 확인 가능한 항목. 04의 테스트 앱 JSON 헬스 규칙은 임의 운영 앱에 강제하지 않는다. |
| E9 Agent 실행 위치 | 운영 Agent runner는 `cmp_providers.onprem_agent`(04 `k3s_agent` 선례)에 두고 04A `AgentControlClient`에 산출물 다운로드를 추가해 재사용한다. 전용 kubeconfig·명시 context를 모든 kubectl/helm 호출에 넘기며 kube-system UID로 대상을 고정한다(운영이므로 `k3d-cmp-*` 제한은 두지 않음). 검증은 격리 k3d에서만 한다. | 공통 렌더(04B)·등록/전송(04A)을 복제하지 않는다. |

### 합의한 Test Seam

- Seam: (1) FastAPI `TestClient` + SQLite 업무 DB의 Agent claim·산출물 다운로드·결과 보고·rollout 조회 경계, (2) `temporalio.testing.ActivityEnvironment`의 실제 `execute_on_prem_plan` + 로컬 ArtifactStore + 가짜 Agent(실제 claim/report 서비스 함수 호출), (3) 격리 k3d의 실제 Agent runner(Helm·kubectl) 배포·재연결·READY 차단.
- 잡아내는 것: 미승인·거절·오래된 Revision·Digest/산출물 변조 작업의 차단, 동일 Plan Revision 재실행 시 작업 1건 유지, Agent 오프라인 시 작업 보존과 재접속 후 결과 반영, 결과 유실 후 재설치 없는 검증 복구, PVC/워크로드/DB/앱 헬스 실패 시 READY 금지, Secret 원문 미노출.
- 놓치는 것: 08 병합 전 실제 `ProvisioningApprovalWorkflow` 경유 실행(병합 후 확인), 실제 NAT·HTTPS 원격 연결, 다양한 사용자 K3s 배포판·RBAC, 외부 트래픽 경로, S3 산출물 저장소, 장시간 운영 lease.
