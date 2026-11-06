# Ticket 60 — 실제 모드 계획·승인 이벤트와 데모 승인자 발급

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰 대기) — [Issue #233](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/233). 2026-11-06 조율 세션 지시로 착수(본선 웹 실제 모드 연결 묶음 57–62). 담당 민서.

## 배경
웹 실제 모드(SSE `GET /migrations/{id}/events`)의 계획·승인 화면은 `plan.step`·`plan.preview`·`plan.ready`·`approval.required`·`approval.decision`으로 그려진다. 착수 전에는 `plan.ready`·`approval.decision`만 발행됐다. 웹의 "계획 수정 요청"은 승인 API의 REJECT로 보내는데, REJECT는 Migration을 `plan_rejected`로 끝냈고, 같은 입력을 다시 게시해도 `PLAN_REUSED`로 거절된 Revision이 돌아왔다(흐름 점검 N8). 승인 API는 Bearer 승인자가 필요하고 승인자는 Migration ID 단위로 묶여 있어, 웹 BFF가 고정 토큰 하나로 새로 시작한 Migration을 승인할 수 없었다.

## 관련 요구사항
PROV-014(Preview), PROV-015(승인), PROV-021(비밀값 없음). 계약: [migration-events.md](../../specs/provisioning/migration-events.md), [events-guide.md](../../docs/engineering/events-guide.md), 웹 `src/lib/migration/{events.ts,script.ts,reducer.ts,sseProvider.ts}`(`main` `4b96eba`), `contract/events.schema.json`.

## 선행 작업
07(승인), 18(게시·P3 재사용), 22(워크로드 Plan), 24(이벤트), 49(실패 이벤트), 55(샌드박스 결과).

## 작업 범위
- 포함: 계약 사본 동기화(`plan.preview` 백엔드 Preview 모양·`approval.required.pending_decisions`), 게시 Activity의 4종 이벤트, REJECT 뒤 새 Revision 게시와 Workflow 재대기, 데모 승인자 절차 정리(#215 스크립트 사용).
- 제외: 웹 BFF 중계(웹 작업), 분석·샌드박스 Activity, 데이터 전환 승인(`gate: cutover`, 63), `/plan/revise` API.

## 착수 결정 (2026-11-06)

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| A1 계획 수정 API | `/plan/revise`는 만들지 않는다. 웹은 REJECT를 보내고, Migration이 같은 게시 Activity를 다시 불러 새 Revision을 게시한 뒤 그 Revision의 승인을 기다린다 | 이슈 착수 결정 그대로 |
| A2 `plan.step` 의미 | 웹 계약의 `plan.step`은 **매핑 행**(`id·group·grade·before·after·evidence·change·action·keep_source·verification`)이다. "입력 정리→매핑→렌더→Preview" 같은 단계 진행 필드는 계약에 없다(웹 `script.ts`도 매핑 행 6개를 보낸다). 그래서 게시 Activity가 샌드박스가 검증한 분석 보고서(`planning_request.sandbox.report_ref`)의 `mapping[]` 행마다 `plan.step`을 낸다. 변환은 events-guide §2 "계획: 매핑" 규칙(`treatment` 없음 → `APPROVAL`, `KEEP` → `keep_source`, 첫 근거가 CODE면 `code`, `after`는 값이 없어 `""`) | 단계 진행용 새 필드는 웹 계약 변경이라 만들지 않았다. 보고서는 분석·샌드박스와 같은 sha256 저장소(`ARTIFACT_STORE_DIR`)에서 읽는다. 읽지 못하면 `plan.step` 없이 나머지를 내고 운영 로그 `PLAN_STEP_SOURCE_UNAVAILABLE`을 남긴다 |
| A3 `plan.preview` 모양 | 저장된 Revision의 백엔드 Preview(`contract_version·manifest_digest·changes·applied_defaults·warnings·errors·pending_decisions`) 그대로 + `plan_id`·`revision`·`target{provider, platform(EKS/GKE/K3s), location(region 또는 environment_id)}`·`security_rules`. 웹이 `changes`가 배열이면 `previewAdapter`로 읽는다(`data:plan.preview.backend`). `source`·`env_remap`은 백엔드에 값이 없어 보내지 않는다(선택 필드) | 웹 계약이 이미 이 모양을 받는다. 화면용 `resources[]`로 바꾸는 일은 웹 어댑터가 한다(한 곳에서만 변환) |
| A4 보안 규칙 | 검증된 spec의 `network_intent` + 플랫폼이 붙이는 앱→DB(TCP 5432, 26 `with_connections`)를 `ALLOW`로, 앞에 외부 진입 → 앱 포트 `ALLOW`, 끝에 `* → * ANY DENY`. 26 NetworkPolicy(기본 차단 + 허용)와 같은 뜻이다 | 이름·포트만 담는다 |
| A5 `approval.required` 값 | `{excluded: KEEP 행 id, rollback_policy: "", pending_decisions: Preview.pending_decisions}`, phase `awaiting_approval`, status `waiting`. 이슈의 "(plan_id, revision, digest)"는 웹 계약의 `approval.required`에 없는 필드라(`additionalProperties: false`) 넣지 않는다. 웹은 승인 대상 식별자를 직전 `plan.ready`에서 읽는다(`sseProvider.plan`) | 계약을 바꾸면 웹 스키마도 바뀐다. 같은 값이 바로 앞 `plan.ready`에 있다 |
| A6 발행 위치·순서 | 게시 Activity(`publish_migration_plan`·`publish_workload_plan`)의 `PlanEvents` 래퍼가 게시 성공(`PUBLISHED`) 직후 `plan.step`* → `plan.preview` → `plan.ready` → `approval.required` 순서로 낸다(웹 `planTimeline` + mock `awaiting_approval` 순서). Workflow는 이 결과를 받자마자 승인 대기에 들어가므로 Workflow 수준 이벤트(추가 Activity·이력 변경)를 만들지 않았다. idempotency_key는 `<plan_id>:<revision>:step:<row>`·`:preview`·`:required` | Workflow 결정성에 영향이 없다. AWS/GCP는 인프라 Plan과 워크로드 Plan에서 한 번씩 나오고, 같은 행 id는 웹이 덮어쓴다 |
| A7 재사용 Revision | `PLAN_REUSED`(같은 입력 재게시, Activity 재시도 포함)도 같은 값을 다시 낸다 | 첫 시도가 게시 후 이벤트 전에 끊긴 경우에도 화면이 채워진다. 웹은 같은 id·같은 Revision을 덮어쓴다 |
| A8 REJECT 뒤 새 Revision (N8) | 18 P3를 한 줄 보탠다: **최신 Revision에 REJECT 결정이 있으면 같은 입력이어도 재사용하지 않고 최신+1을 게시**한다. 거절된 Revision은 다시 승인할 수 없으므로(`ALREADY_DECIDED`) 재사용할 이유가 없다. "승인은 최신 Revision만"(P3 근거)과 맞고, 새 Revision 뒤 같은 입력은 다시 `PLAN_REUSED` | 최소 변경. 클라우드 대상은 새 Revision 디렉터리로 Terraform Plan을 다시 만든다(저장 Plan 재사용 아님) |
| A9 Workflow | `workflow.patched("plan-reject-republish-v1")` 아래 `_approve_revisions`가 결과 `plan_rejected`이면 같은 게시 Activity를 다시 부르고 새 Revision 승인을 기다린다. 재게시가 거절된 Revision을 그대로 돌려주면(정책상 새 Revision 불가) `plan_code=PLAN_REJECTED`·`plan_rejected`로 끝나고 49의 `error` 이벤트(`PLAN_REJECTED`)가 나간다. 세 번째 REJECT도 `plan_rejected`(무한 반복 방지). 취소 요청 중이면 재게시하지 않는다. 인프라·워크로드 Plan 모두 같은 규칙. patch 이전 이력은 기존대로 끝난다 | 거절을 "수정 요청"으로 받으면서 거절의 원래 의미(실행하지 않음)를 지킨다 |
| A10 데모 승인자 | 승인자 범위(Migration ID 목록)는 그대로 둔다. 고정 토큰 1개가 새로 시작한 Migration을 승인하도록 07의 운영자 API `POST /provisioning/approvers/{approver_id}/migrations`로 실행마다 Migration을 더한다. 발급·추가·교체·폐기 스크립트와 절차는 origin/main에 먼저 병합된 [`infra/demo/approver-token.py`](../../infra/demo/approver-token.py)·[infra/demo README](../../infra/demo/README.md#데모-승인자-토큰-웹-platform_approver_token)(#215)를 그대로 쓴다. 이 티켓은 같은 일을 하는 `scripts/` 스크립트와 grant API를 만들었다가 origin/main 병합 때 중복이라 지웠다 | 같은 일을 하는 스크립트가 둘이면 운영자가 헷갈린다. 대안(Migration마다 새 승인자, 전체 Migration 와일드카드)은 하지 않았다(와일드카드는 권한 확대) |
| A11 토큰 취급 | 토큰을 표준 출력에 내지 않으려면 `create`·`rotate`에 **반드시 `--env-file /opt/cmp/web.env`**를 붙인다(0600 파일에 `PLATFORM_APPROVER_TOKEN`으로만 쓰고 출력하지 않는다). 운영자 키는 환경 변수 `APPROVAL_ADMIN_KEY` 또는 숨김 프롬프트로만 받는다 | 이슈 범위. `--env-file` 없이 실행하면 토큰이 표준 출력에 한 번 나온다(#215 동작, `infra/demo/*`는 이 티켓에서 수정하지 않음) |

## 데모 승인자 절차
[infra/demo README "데모 승인자 토큰"](../../infra/demo/README.md#데모-승인자-토큰-웹-platform_approver_token) 그대로(요약):
1. SSM 세션에서 `export "$(grep '^APPROVAL_ADMIN_KEY=' /opt/cmp/.env)"`로 운영자 키를 환경 변수로만 둔다.
2. 한 번: `python3 infra/demo/approver-token.py create demo-approver --migration demo-placeholder --env-file /opt/cmp/web.env` → 웹 재생성(`up -d --force-recreate web`). 토큰은 `web.env`(0600)의 `PLATFORM_APPROVER_TOKEN`에만 있다.
3. 데모 실행마다 Plan이 게시되면 `approver-token.py grant demo-approver --migration <migration-id>`.
4. 웹 BFF(웹 작업)가 `PLATFORM_APPROVER_TOKEN`으로 `POST /provisioning/plans/{plan_id}/revisions/{revision}/approvals`를 중계한다. 계획 수정 요청은 같은 경로에 `decision: REJECT`.
5. 데모가 끝나면 `revoke`.

## 수용 기준 (Issue #233)
- [x] 새 이벤트 계약 모델 검증 — `plan.preview`(백엔드 모양 `data:plan.preview.backend`, oneOf)·`approval.required.pending_decisions`를 웹 `contract/events.schema.json`(`main` `4b96eba`)과 같게 맞추고 `tests/test_event_contract.py`가 정의별로 비교.
- [x] 중복 없음 — 같은 Activity 시도 안에서 idempotency_key로 한 번(A6). 재시도·재사용은 같은 값(A7).
- [x] 비밀값 비노출 — `plan.preview`는 저장된 Preview(이름·참조만)와 식별자·규칙만 담는다(테스트가 저장 Preview와 비교). 승인자 토큰은 출력되지 않는다.
- [x] 순서는 웹 script.ts와 같음 — `plan.step`×N → `plan.preview` → `plan.ready` → `approval.required`.
- [x] REJECT 뒤 새 Revision의 `plan.ready`가 온다(A8·A9).

## Test Seam
- `tests/test_plan_approval_events.py`: 순수 `plan_step` 변환. 실제 `PlanPublisher`(ON_PREM, SQLite·로컬 ArtifactStore·Helm 렌더) + `PlanEvents` → 이벤트 순서·값, 보고서 없음, 게시 실패, 재사용, REJECT 뒤 Revision 2. Temporal time-skipping 실제 `MigrationWorkflow`·`ProvisioningApprovalWorkflow` + 가짜 Provisioning Activity: REJECT → 재게시 → 새 Revision 승인 → 실행, 재게시가 같은 Revision → `plan_rejected`, 세 번째 REJECT → `plan_rejected`, Replayer.
- 데모 승인자: #215의 `tests/test_demo_approver_token.py`·`tests/test_approval_gate.py`가 스크립트·grant API를 확인한다(이 티켓 변경 없음).

## 검증
- RED: 새 테스트 파일이 `PlanEvents`·`plan_step` 없음으로 실패.
- GREEN: `uv run pytest tests/test_plan_approval_events.py tests/test_event_contract.py tests/test_migration_plan_workflow.py tests/test_cloud_workload_workflow.py tests/test_provisioning_events.py tests/test_approval_gate.py`
- `make check`, alembic(SQLite) check, `make openapi`: PR 본문에 결과를 적는다.

## 남은 일
- 웹 BFF 승인 중계와 `PLATFORM_APPROVER_TOKEN`(웹 작업).
- 데모 실행마다 `grant`가 필요하다(A10). `--env-file` 없는 `create`·`rotate`는 토큰을 출력한다(#215, `infra/demo` 범위). 시작 API가 승인자를 자동으로 묶게 할지는 정책 결정이 필요하다.
- `plan.step`의 `after`(대상 서비스)·`kind`는 매핑 행에 값이 없다(events-guide §5, 분석 담당).
- `plan.preview.source`·`env_remap`은 백엔드 값이 없다. 보고서가 플랫폼 저장소에 없는 배치에서는 `plan.step`이 나오지 않는다(A2).
- 데이터 전환 승인 `approval.required gate: cutover`는 63.
