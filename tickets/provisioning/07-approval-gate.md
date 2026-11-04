# Ticket 07 — Plan Revision 승인과 Temporal 실행 차단

## 상태
IN_PROGRESS — [Issue #34](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/34); 착수 결정 D1~D6과 Test Seam을 아래에 기록

## 목적
승인된 정확한 계획만 실행 경계까지 도달하게 한다.

## 관련 요구사항
PROV-014, PROV-015, PROV-016, PROV-021

## 선행 작업
Blocked by: 01, CP-01

## 작업 범위
- 포함: 업무 DB 승인 기록, Plan/Revision/Digest 확인, Temporal 승인 대기·재개와 안전한 가짜 실행 경계를 구현한다.
- 제외: 실제 Terraform·Helm·Agent Apply

## 수용 기준
- [ ] 미승인·오래된 Revision·변경된 Digest·거절 계획은 실행 경계에 도달하지 않는다.
- [ ] 중복 승인과 DB 기록/Signal 전달 재시도로 동일 계획이 중복 실행되지 않는다.
- [ ] 승인 요청 접수와 Workflow 재개 상태를 구분한다.

## 테스트 전략
- 제안 Test Seam: API 승인 요청과 Temporal Workflow 상태/가짜 executor. 실제 인프라 변경은 확인하지 못한다.
- 실행: Temporal 테스트 환경의 승인 대기·중복·stale Revision·거절·재전달 시나리오; HTTP 변경 시 `make openapi`.
- 실제 AWS/GCP 승인 경계는 `LOCAL_STATE_VALIDATION_ONLY` Preview를 거절한다. 대상 계정/프로젝트 및 중앙 S3 State·잠금이 확인된 최종 저장 Plan과 그 Preview/Digest가 일치해야 승인 기록을 만들 수 있다. 가짜 executor 테스트 통과만으로 운영 승인 가능 상태를 선언하지 않는다.

## 완료 시 확인할 동작
Preview ID/Revision을 승인할 때만 안전한 executor가 호출되는 흐름을 볼 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Preview/승인은 유지한다. DB 승인 기록 + Temporal Signal을 사용하고 범용 Outbox는 보류한다. 승인 이벤트 ID 기반 중복 방지·실패 후 재전달·재조회를 구현한다. 제품 API URL·필드·인증/권한·응답 계약은 착수 전에 담당자와 확정한다.
- 구현 중 변경: 2026-11-04 착수 결정 D1~D6 참조(API·인증·이벤트 ID·Workflow·Plan 저장·실행 경계).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 순차 작업 조건 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)와 [공유 계약](../../specs/provisioning/shared-contracts.md)을 따른다. 사용자 후속 지시에 따라 한 티켓씩 순차 구현하고 관련 문서·SDD/index 수정은 동일 구현 이슈/PR에 포함한다. 공통 계약을 복제하지 않고 기존 모델을 사용한다.

필수 시나리오: DB 승인 기록 성공 후 Signal 실패, 같은 승인 재요청/재전달, 승인자 권한 없음, 오래된 Revision/Digest, 실행 전 재확인. 전달 실패를 접수/실행 완료로 숨기지 않는다.

## 착수 결정 (2026-11-04)

사용자 지시: 기획 문서의 가장 최근 결정을 우선 적용하고, 문서에 없는 항목은 구현자가 최선안을 선택해 여기에 기록한다. 아래 D1~D6은 ADR-002 #8의 "07 착수 조건"을 이 방식으로 확정한 것이다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 API | `POST /provisioning/plans/{plan_id}/revisions/{revision}/approvals` (body: `decision`=APPROVE/REJECT, `scope`, `manifest_digest`, `preview_digest`). 새 기록 201, 같은 결정 재요청 200(기존 기록 반환). `GET .../approvals/{approval_id}` 재조회, `POST .../approvals/{approval_id}/delivery` 재전달. 응답은 `approval_id`, 결정, `delivery_status`(PENDING/DELIVERED/FAILED), `delivery_attempts`, `workflow_state`(Query 결과, 조회 불가 시 UNAVAILABLE)를 분리한다. 오류: 401 `APPROVER_AUTH_REQUIRED`, 403 `APPROVER_FORBIDDEN`, 404 `PLAN_NOT_FOUND`/`APPROVAL_NOT_FOUND`, 409 `STALE_REVISION`/`APPROVAL_TARGET_MISMATCH`/`ALREADY_DECIDED`, 422 `PLAN_NOT_APPROVABLE`(+`approval_issues` 코드). 전달 실패는 오류가 아니라 201/200 + `delivery_status=FAILED`로 드러낸다. | URL이 승인 대상 Plan ID·Revision을 직접 식별(ADR-002 #8). 기존 `/migrations/{id}/approve` 확장은 Workflow ID 기준이라 제외. |
| D2 인증·권한 | 승인자별 Bearer 자격 증명. 운영자가 `X-Approval-Admin-Key`(`APPROVAL_ADMIN_KEY`, 04A와 같은 hmac 비교·미설정 시 403)로 `POST /provisioning/approvers`를 호출해 승인자 ID와 허용 `migration_id` 목록을 등록하고 자격 증명을 한 번만 받는다. DB에는 SHA-256 digest만 저장한다. 승인자 신원은 자격 증명에서만 결정하고 요청 JSON의 신원은 받지 않는다. 권한 = 해당 Plan의 `migration_id`에 대한 grant. | 04A 등록 패턴 재사용. 공유 운영자 키 하나는 승인자 추적(PROV-015)이 약해 제외. 실제 사용자 인증 체계 연동은 후속. |
| D3 이벤트 ID·중복 | `approval_id`는 서버 UUID. `(plan_id, revision)`당 결정 1건(DB unique). Signal은 `approval_id`만 전달하고 Workflow는 처리한 ID를 무시한다. 실행 경계는 `(plan_id, revision, step)` unique 실행 표식을 같은 트랜잭션에서 기록해 executor를 정확히 한 번 호출한다. 접수(DB 결정)·전달(delivery_status)·Workflow 상태를 별도 필드로 둔다. | ADR-002 #9. 결정적 해시 ID·Idempotency-Key 단독 방식은 승인자/시점 추적 또는 중복 차단이 약해 제외. |
| D4 Workflow | 스캐폴드 `MigrationWorkflow`는 바꾸지 않고 `ProvisioningApprovalWorkflow`(ID `provisioning-approval-{plan_id}-r{revision}`, queue `migration`)를 추가한다. API는 Signal-With-Start로 전달해 Workflow 미기동/기동 상태 모두 같은 경로로 복구한다. 재검증·실행은 `provisioning` queue Activity가 DB에서 수행한다. | analysis-verification plan(2026-11-04)에서 02/02B가 `workflows/migration.py`를 변경 예정이므로 충돌 회피. 상위 Migration 연결은 후속 통합. |
| D5 Plan 저장 | 업무 DB `provisioning_plan_revisions`(plan_id, revision, migration_id, manifest JSON, preview JSON, 게시 시각). Revision은 불변이며 같은 Plan의 최대 Revision이 활성이다. 게시는 내부 함수 `publish_plan_revision`(HTTP 없음)로 하며 Planning 연결은 후속. 저장 전 `PlanManifest`/`Preview` 계약 검증을 통과해야 한다. | 승인 시 "현재 활성 Revision"을 조회할 원천이 없었음. Preview는 이미 민감정보 제거 계약이고 Terraform/State 원문은 저장하지 않는다. |
| D6 실행 경계 | `execute_approved_plan` Activity가 실행 직전 같은 트랜잭션에서 승인 기록(APPROVED)·최신 Revision·`verify_approval_target`(저장된 manifest/preview 재계산)을 확인하고, 실패 시 실행 표식 없이 BLOCKED 코드를 반환한다. 성공 시 주입된 executor를 호출한다. 기본 executor는 표식만 남기는 안전한 no-op이며 Terraform·Helm·Agent를 호출하지 않는다. | plan.md §10, shared-contracts의 "클라이언트 target만으로 실행 금지". |

### 합의한 Test Seam

- Seam: (1) FastAPI `TestClient`의 승인 HTTP 요청 + SQLite 업무 DB, (2) `temporalio.testing.WorkflowEnvironment`의 실제 `ProvisioningApprovalWorkflow`와 실제 재검증 Activity + 호출을 기록하는 가짜 executor, (3) Temporal 전달 실패를 일으키는 가짜 delivery 포트(API 의존성 주입).
- 잡아내는 것: 미승인·거절·오래된 Revision·Digest 변경·`LOCAL_STATE_*` Preview의 실행 차단, 승인자 인증/권한, 재요청·재전달·중복 Signal에서도 executor 1회, DB 기록 후 Signal 실패의 FAILED 노출과 재전달 복구, 접수와 Workflow 상태 분리, 실행 직전 재확인.
- 놓치는 것: PostgreSQL 고유 동작(Alembic 마이그레이션은 별도로 SQLite/PG에서 upgrade 확인), 실제 Temporal 서버 장애 유형(로컬 `make smoke-temporal`로 일부 보완), 실제 Terraform/Helm/Agent 부수효과, 실제 사용자 인증 체계, 중앙 S3 State·운영 Plan의 실제 승인 가능성.
