# Ticket 18 — Plan 생성·게시와 Preview 조회 연결 (세 대상)

## 상태
DONE — [Issue #98](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/98). 2026-11-05 조율 세션 지시로 아래 착수 결정을 기록하고 구현했다. 자체 리뷰 완료, 조율 세션 리뷰·PR 대기. 2026-11-05 Provisioning 공백 점검에서 범위를 ON_PREM 전용에서 세 대상과 Preview 조회까지 넓혔다.

## 목적
승인(07)과 실행(08~12)은 게시된 Plan Revision이 있어야 시작되는데, 지금 Plan을 만들고 게시하는 운영 코드가 없다. 테스트와 `scripts/`만 Plan을 게시한다. 또 승인 요청에 `preview_digest`가 꼭 필요하지만 Preview를 돌려주는 API가 없어 승인자가 Preview를 볼 수 없다. Planning 입력 v2에서 대상별 Plan·Preview를 만들어 게시하고, 승인자가 Preview를 조회해 승인하는 경로를 만들어 "이전 요청 → Plan·Preview → 승인 → 실행"을 잇는다.

## 관련 요구사항
PROV-004, PROV-005, PROV-007, PROV-008, PROV-009, PROV-014, PROV-015, PROV-021

## 선행 작업
Blocked by: 01A, 04B, 05, 06, 07, 12, 12A, 15 (DONE)

## 현재 구현과 재사용 지점
- `cmp_worker.provisioning_v2.prepare_plan_v2`: Planning 입력 v2 검증(순수 함수)
- `plan_aws_infrastructure`·`plan_gcp_infrastructure`(05·06): `scripts/`·테스트에서만 호출
- `cmp_providers.onprem_preview.onprem_preview`(12A): 운영 호출 없음
- `tests/onprem_rollout_support.py`: Chart 패키징·Values 직렬화·`ArtifactStore.put`·`PlanManifest` 구성 — 테스트 전용
- `cmp_api.approvals.publish_plan_revision`: 운영 호출 없음, HTTP route 없음
- `MigrationWorkflow`의 `planning`(`prepare_plan`)·`provisioning`(`provision_target`) 단계: scaffold("No Terraform plan generated")

## 작업 범위
- 포함
  - 대상별 Plan 생성 Activity(`provisioning` queue): Planning 입력 v2 + 대상 설정 → AWS/GCP는 Terraform Plan·Preview(05·06), ON_PREM은 v2 렌더·Chart 패키징·`onprem_preview`(12A) → 산출물 저장 → `PlanManifest` → `publish_plan_revision`. 같은 입력의 재실행은 같은 Revision을 다시 만들지 않는다.
  - ON_PREM 대상 검증: Agent가 등록돼 있고 폐기되지 않았으며 cluster UID가 등록 값과 같을 때만 게시한다.
  - Preview 조회 API: 승인자 인증(07)으로 `plan_id`·`revision`의 Preview 본문과 `preview_digest`를 반환한다. 비밀값은 포함하지 않는다.
  - `MigrationWorkflow`의 `planning`·`provisioning` 두 단계를 실제 Activity(Plan 게시 → 승인 대기 → 실행 결과)로 교체한다. 다른 단계는 건드리지 않는다.
  - SDD·OpenAPI·Temporal 흐름 문서 갱신
- 제외: S3 ArtifactStore(21에서 결정), 프론트 Preview 화면, 다른 팀 단계(분석·Sandbox·데이터·트래픽)의 연결, `.env` 값 주입(20)

## 수용 기준
- [x] 세 대상 각각 정상 입력으로 게시한 Revision의 Preview가 05·06·12A 계약을 통과하고, 승인 후 08·10·12 실행이 시작된다. (ON_PREM은 Preview API → 승인 → claim → `execute_on_prem_plan` 작업 생성까지, AWS/GCP는 `INFRASTRUCTURE` 게시·`approval_issues` 통과·08/10이 읽는 저장 Plan 디렉터리까지. AWS/GCP claim 후 `execute_aws_plan`/`execute_gcp_plan` 선택은 기존 `execution_steps` 그대로)
- [x] Preview 조회 API가 승인 권한이 있는 승인자에게만 Preview와 Digest를 돌려주고, 그 Digest로 승인할 수 있다.
- [x] 등록되지 않은·폐기된 Agent, cluster UID 불일치, 렌더·Plan 실패는 게시하지 않고 구조화 오류를 반환한다.
- [x] 같은 입력의 재실행이 중복 Revision을 만들지 않고, 입력이 바뀌면 새 Revision이 된다.
- [x] `MigrationWorkflow`가 Plan 게시·승인 대기·실행 결과를 실제로 거치고, 승인 거절·만료 시 실행하지 않는다.
- [x] 산출물·Preview·API 응답·로그·Temporal 이력에 Secret 원문이 없다. (입력 계약에 Secret 값 필드가 없고, DB 비밀번호는 Secret 이름·키만, Terraform 출력은 Preview·결과에 없음을 테스트로 확인. Activity·Workflow 이력에는 ID·코드·Digest만)

## Test Seam 후보
- SQLite 업무 DB + 로컬 ArtifactStore + 실제 Helm 패키징으로 ON_PREM 생성 Activity → Preview API → 승인 → `execute_on_prem_plan`(가짜 Agent).
- AWS/GCP는 05·06의 가짜 Terraform 실행기로 Plan 생성·게시까지.
- Temporal 테스트 환경으로 `MigrationWorkflow` 두 단계(승인·거절·만료).
- 놓치는 것: 실제 클라우드 Plan(05·06에서 확인됨), 실제 서버 실행(21).

## 검토 시 정할 것
- 대상 설정(Agent ID·Namespace·StorageClass·리전 등)을 누가 어떤 입력으로 주는지와 Planning 담당 경계
- 재실행 판정 기준(입력 Digest)
- `MigrationWorkflow` 단계 교체를 Migration 소유자와 확인할지

## 착수 결정 (2026-11-05)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| P1 입력 출처·담당 경계 | Planning 입력은 Migration별 **Planning 입력 등록**(업무 DB `provisioning_planning_inputs`, `PUT /provisioning/migrations/{migration_id}/planning-input`, 운영자 키 `X-Approval-Admin-Key`)으로 받는다. 본문은 `{contract_version: "1", planning_request: PlanningRequestV2, target: 대상 설정, requirements_json?}`. 대상 설정은 Provider별 판별 모델: ON_PREM `{environment_id, agent_id, cluster_uid, namespace, storage_class, database_image}`, AWS `{settings: AwsPlanInput에서 migration_id·plan을 뺀 필드}`, GCP `{settings: GcpPlanInput에서 migration_id·plan을 뺀 필드}`. Provider는 `target_selection.provider`와, AWS/GCP 리전은 `target_selection.location`과 같아야 한다. 요구사항 바이트는 플랫폼 ArtifactStore(`ARTIFACT_STORE_DIR`, SHA-256 파일명)에서 읽고, 없으면 등록 본문의 `requirements_json`(정확한 UTF-8 바이트, SHA 일치 필수)을 저장한다. 비밀값 필드는 없다 | Analysis·Sandbox·Build 단계가 아직 scaffold라 상위 Workflow가 v2 입력을 만들지 못한다. 그 단계의 연결은 다른 팀 범위라 Provisioning이 소유한 등록 경계만 연다. `MigrationRequest`·Workflow 시작 계약은 바꾸지 않는다. 대상 설정을 Planning 입력 v2에 넣는 대안은 01A 계약 변경이라 피했다. (2026-11-06 [44](44-web-start-to-planning.md#착수-결정-2026-11-06): 웹 시작 요청이 대상 Profile 카탈로그로 이 대상 설정을 만들어 Workflow 인계로 등록한다. 등록 계약은 그대로다) |
| P2 Plan ID·Scope | Plan ID는 `plan-` + `sha256(migration_id)` 앞 24자(Migration당 하나). ON_PREM은 `WORKLOADS`(기존 K3s), AWS/GCP는 05·06 Plan 그대로 `INFRASTRUCTURE`. AWS/GCP 워크로드 Plan(09·11용 `WORKLOADS`/`FULL` 게시)은 후속 | 수용 기준이 08·10·12 실행 시작이다. AWS/GCP FULL은 05·06 증거 파일(`evidence.json`의 manifest Digest)과 08/10 Apply 검사를 바꿔야 해서 이 티켓에서 제외했다 |
| P3 재실행 판정 | 입력 Digest = `sha256_json({contract_version, planning_request, target, requirements_sha256, provider_template})`. 최신 Revision의 입력 Digest와 같으면 그 Revision을 그대로 돌려준다(`PLAN_REUSED`). 다르면(또는 과거 Revision과만 같으면) 최신+1 새 Revision. 입력 Digest는 `provisioning_plan_publications`에 Revision과 같은 트랜잭션으로 저장한다. AWS/GCP는 Plan 디렉터리가 이미 있고 DB Revision이 없으면(게시 직전 Worker 종료) `spec.json` 바이트가 같을 때만 그 manifest·Preview를 게시하고, 다르면 `PLAN_DIRECTORY_CONFLICT` | 같은 입력 재실행이 Terraform Plan을 다시 돌리지 않는다. 승인은 최신 Revision만 가능하므로 A→B→A는 새 Revision이어야 한다. 2026-11-06 [60](60-plan-approval-events.md) A8: 최신 Revision에 REJECT 결정이 있으면 같은 입력이어도 재사용하지 않고 최신+1을 게시한다(거절된 Revision은 다시 승인할 수 없다). 클라우드 State 변화는 저장 Plan Apply가 stale로 거절한다 |
| P4 ON_PREM 검증·렌더 | 게시 전 Agent 등록(`AGENT_NOT_REGISTERED`)·폐기(`AGENT_REVOKED`)·cluster UID(`CLUSTER_UID_MISMATCH`)를 확인한다. P0는 PostgreSQL 1개·앱 1개(`UNSUPPORTED_TOPOLOGY`). Release 이름은 리소스 ID 소문자(`_`→`-`), DB 비밀번호 Secret은 `<DB release>-auth`/`password`(Agent가 생성). 앱 env는 요구사항의 리소스 출력 바인딩만(사용자 `.env` 값은 20). Chart 패키징은 테스트 전용 코드를 `cmp_providers.chart_package`로 옮긴다. SPEC은 `{spec, target, deployment}` 정규 JSON, BINDINGS는 `{"bindings": []}` | 12의 `build_rollout_job` 조건과 같다. 04B·12A·12를 그대로 재사용한다 |
| P5 게시 차단 | 입력 검증·Agent·렌더·Plan 실패, Preview `errors`가 있으면 게시하지 않고 `{status: FAILED, code, issues: [code]}`를 반환한다. `pending_decisions`(예: `CLEANUP_POLICY_REQUIRED`)는 Preview에 넣고 게시하되 07이 승인을 거절한다 | 05의 로컬 검증 Plan과 같은 규칙. 오류 메시지·경로에 입력 값을 넣지 않는다 |
| P6 Preview 조회 API | `GET /provisioning/plans/{plan_id}/revisions/{revision}/preview`와 `GET /provisioning/migrations/{migration_id}/plan`(최신 Revision). 07 승인자 Bearer 인증, Migration 권한 없으면 403. 응답 `{plan_id, revision, latest, migration_id, provider, scope, manifest_digest, preview_digest, approvable, approval_issues, preview}`. `preview_digest`는 07과 같은 `sha256_json(preview)` | 승인 요청에 그대로 넣을 Digest를 준다. 저장된 Preview는 05·06·12A가 비밀값 없이 만든 것만 담는다 |
| P7 Workflow 연결 | `MigrationWorkflow`의 `planning` 단계를 `workflow.patched("provisioning-plan-v1")` 아래 `publish_migration_plan` Activity로 바꾼다. `PUBLISHED`면 `ProvisioningApprovalWorkflow`를 Revision Workflow ID(`approval_workflow_id`)의 자식으로 시작해 승인 대기·실행 결과를 기다리고, API의 Signal-With-Start가 그 자식에 승인을 전달한다. 자식 결과 `EXECUTED`만 다음 단계로 간다. 거절 `plan_rejected`, 만료 `approval_expired`, 그 외 `provisioning_failed`, 게시 실패 `plan_failed`로 끝낸다. 플랫폼 ArtifactStore가 설정되지 않은 환경은 Activity가 `SCAFFOLD`를 돌려 기존 scaffold 승인 Signal·`provision_target` 경로를 그대로 쓴다. 기존 이력은 patch 표식이 없어 기존 경로로 재생된다 | 다른 팀 단계와 기존 scaffold 테스트·smoke를 건드리지 않는다. 분석 실제 경로(19)도 설정이 있을 때만 켜는 같은 방식이다 |
| P8 승인 만료 | 부모가 자식 요청에 `approval_timeout_seconds`(기본 7일)를 넣고, 자식은 그 안에 승인이 없으면 `EXPIRED/APPROVAL_EXPIRED`로 **정상 종료**한다. 닫힌 Workflow라 늦은 승인 전달은 재시작되지 않아 실행이 없다. API가 직접 시작하는 기존 요청(필드 없음)은 무기한 대기 그대로 | 만료 기간은 정책 문서에 없어 기본값만 정했다(정책 변경 아님). 실행 중에는 만료가 걸리지 않게 승인 수신 전까지만 센다 |
| P9 Migration 소유자 확인 | `planning`·`provisioning` 두 단계만 바꾸고 다른 단계·Workflow 시작 계약·`MigrationStatus`는 그대로 둔다. 상태 Query에 `plan`(ID·Revision)·`plan_code`만 추가한다(API 응답 모델은 추가 키를 무시). PR 리뷰에서 Migration 소유자에게 알린다 | 응답 계약 변경이 없어 사전 합의가 필요하지 않다 |

## 구현 요약
- 계약: `cmp_contracts.plan_publish`(`PlanningInputRegistration`, Provider별 대상 설정, `plan_id_for`, patch·만료 상수)
- 등록: `cmp_api.plan_inputs`(`provisioning_planning_inputs`, 입력 Digest), `PUT /provisioning/migrations/{migration_id}/planning-input`
- 게시: `cmp_worker.plan_publish.PlanPublisher`, Activity `publish_migration_plan`(`cmp_worker.activities.plan_publish`, `provisioning` queue), `publish_plan_revision(..., input_digest=)` + `provisioning_plan_publications`
- Chart 패키징: `cmp_providers.chart_package.package_chart`(테스트 전용 코드 승격, `tests/onprem_rollout_support.py`가 재사용)
- 조회: `GET /provisioning/plans/{plan_id}/revisions/{revision}/preview`, `GET /provisioning/migrations/{migration_id}/plan`
- Workflow: `MigrationWorkflow` `planning`(patch `provisioning-plan-v1`) → 자식 `ProvisioningApprovalWorkflow`(선택 `approval_timeout_seconds`)
- DB: Alembic `20261105_04`. 기존 `env.py`가 16의 터널 테이블을 등록하지 않아 `alembic check`가 실패하던 문제도 함께 고쳤다(import 1줄)

## 검증 기록 (2026-11-05)
- `make check`: ruff lint/format 통과, `pytest` 978 passed, 4 skipped
- 신규 `tests/test_plan_publish.py`(20): ON_PREM 게시·재사용·새 Revision·Agent 미등록/폐기/UID 불일치·입력 오류·토폴로지·렌더 실패·미등록 입력·scaffold Activity·Preview API(401/403/404, 최신 조회)→승인→claim→`execute_on_prem_plan` 작업 생성·등록 API(운영자 키, Provider/Digest/Migration 불일치)·AWS 게시·게시 직전 종료 후 재시도(Terraform 재실행 없음)·AWS Plan 실패/리전 불일치·GCP 게시·클라우드 프로필 미설정·Sandbox 정리 미결정(게시하되 승인 불가)·Secret 참조만 저장
- 신규 `tests/test_migration_plan_workflow.py`(6, Temporal time-skipping + Replayer): API Signal-With-Start 승인 → claim·실행·complete, 기존 boolean Signal 무시, 거절, 만료 후 늦은 승인 무실행, 게시 실패, 승인 대기 중 취소, 미설정 scaffold 경로
- 기존 회귀: 07/08 승인 Workflow, 12 온프레미스 배포, 분석·Sandbox 부모 Workflow 테스트와 기존 이력 Replay 통과
- `make openapi`: 경로 3개·스키마 추가만 있는 diff 검토
- `alembic upgrade head` → `alembic check`(No new upgrade operations) → `downgrade 20261105_03` (SQLite)

## 미검증
- `make smoke-temporal`: 로컬 Temporal(7233)·Docker가 꺼져 있어 실행하지 못했다. Workflow는 Temporal 테스트 서버에서만 확인했다. 로컬 `.env`에 `ARTIFACT_STORE_DIR`가 설정돼 있으면 smoke는 실제 경로를 타고 Planning 입력이 없어 `plan_failed`로 끝난다(scaffold smoke는 이 값을 비우고 실행)
- 실제 AWS/GCP Terraform Plan(05·06에서 확인됨)과 실제 서버의 Agent 실행(21)은 이번에 돌리지 않았다. 클라우드 Apply·비용 없음
- PostgreSQL 업무 DB에서의 동시 게시 경합(같은 Revision 동시 게시 시 `REVISION_CONFLICT` 후 Activity 재시도로 재사용)은 SQLite로만 확인
- AWS/GCP 워크로드 Plan(09·11 `WORKLOADS`/`FULL`) 게시는 범위 밖(P2). 현재 클라우드 Migration은 인프라 Apply 뒤 워크로드 배포 없이 다음 scaffold 단계로 간다
- 만료 후에도 API는 승인 결정을 DB에 기록할 수 있다. 실행은 일어나지 않고 `workflow_state`가 `EXPIRED`로 보인다

## 후속
- AWS/GCP 워크로드 Plan 게시(09·11 연결)는 [22](22-cloud-workload-plan.md)(#108)로 분리, `.env` 값 주입(20), S3 ArtifactStore(21), Analysis·Sandbox·Build 결과로 Planning 입력을 자동 등록하는 상위 연결(각 담당과 합의)

## 관련 문서
- [05](05-aws-preview.md) · [06](06-gcp-preview.md) · [07](07-approval-gate.md) · [12](12-k3s-approved-rollout.md) · [12A](12a-onprem-preview.md) · [04B](04b-shared-render-v2.md) · [공유 계약](../../specs/provisioning/shared-contracts.md)

## 설계 변경 기록
- 2026-11-05: 15 범위 검토 중 "플랫폼이 발급하는 작업"의 출처가 비어 있음을 확인해 분리했다.
- 2026-11-05: 공백 점검에서 AWS/GCP도 같은 공백이고 Preview 조회 API가 없음을 확인해 범위를 넓혔다.
- 2026-11-05: 착수 결정 P1~P9. AWS/GCP는 `INFRASTRUCTURE`만 게시하고 워크로드 Plan은 후속으로 남겼다. 공유 계약·Temporal 흐름·plan·tasks 문서를 갱신했다. spec.md 요구사항 변경 없음

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영 (ADR 변경 없음)
- [x] 구현·계약 정합성 및 최종 리뷰 (자체 리뷰. 조율 세션 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록

## 후속
- [49](49-failure-visibility.md)(#184): Plan 게시 실패의 `issues`가 `failure_issues`·운영 로그 `MIGRATION_FAILED`로 보존되고, 실패 종료에 웹 `error` 이벤트가 나간다.
