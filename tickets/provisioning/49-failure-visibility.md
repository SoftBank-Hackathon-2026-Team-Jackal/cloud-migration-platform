# Ticket 49 — 실패 이벤트·실패 사유 보존과 running 고착 해소

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 조율 세션 리뷰·병합 대기) — [Issue #184](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/184). 2026-11-06 조율 세션 지시로 착수(1차 병렬 묶음 43·42·51·49). 24 후속.

## 배경 (실동작 점검 D-04·D-05, 흐름 점검 N3·N7)
- D-04: Provisioning·파이프라인이 실패해도 `migration_events`·`migration_logs`가 0건이다. Plan 게시 실패 사유(`issues`, 예: `MISSING_PROVIDER_LOCK`)를 Workflow가 버리고, 상태 API(`GET /migrations/{id}`)는 `plan_code`·`failure_code`도 응답에서 뺀다.
- D-05: Temporal에서 Failed가 된 Migration이 상태 API·SSE에서 계속 `analysis`/`running`이다. SSE는 `status` query의 phase만 보고 실행 종료 상태(FAILED·TERMINATED·TIMED_OUT)를 보지 않는다. 정상 종료한 `plan_failed`도 snapshot phase가 `analysis`로 나간다.
- N3: SSE 상태 대응에 `planning_handoff`·`workload_planning`이 없어 정상 진행 중에 `failed`로 닫힌다.
- N7: 실행 표식(`provisioning_executions`)이 RUNNING으로 영구히 남는 경로 두 개. (a) claim이 표식을 commit한 뒤 응답이 유실돼 Activity가 재시도하면 같은 승인인데도 `ALREADY_EXECUTED`가 되어 실행되지 않는다. (b) complete가 3회 실패하면 승인 Workflow가 실패하고 표식이 RUNNING으로 남는다.
- 범위 이관: #158(티켓 34, 진행 이벤트 추가)의 "Workflow 실패 종료 `error`" 항목을 이 티켓으로 옮겼다. 34 티켓 문서는 main에 없으므로 이 문서에 이관 사실을 기록한다. 34에는 진행 이벤트만 남는다.

## 관련 요구사항
PROV-014, PROV-015, PROV-016, PROV-018, PROV-021. 명세: [migration-events.md](../../specs/provisioning/migration-events.md)(E11·E18~E21).

## 선행 작업
Blocked by: 07, 18, 22, 24 (main 반영). 같은 파일을 44(시작 요청)·50(인계)이 다음 차례에 고친다.

## 작업 범위
- 포함
  - `MigrationWorkflow` 실패 종료 시 웹 계약 `error` 이벤트 발행(비밀 없음)과 `migration_logs` 실패 기록(`issues` 포함)
  - 실패 단계·코드·사유 코드를 Workflow `status` query에 보존(`failure_code`·`failure_phase`·`failure_issues`)
  - 상태 API·SSE가 Temporal 실행 종료 상태(FAILED·TERMINATED·TIMED_OUT·CANCELED)를 반영
  - Workflow phase → 웹 phase 대응 단일화(N3), snapshot phase가 실패 단계를 따름
  - 실행 표식: claim 재시도 멱등(같은 승인 Workflow 실행), complete 장기 재시도와 실패 시 UNKNOWN 결과(N7)
- 제외: 웹 시작 입력(44), 진행 이벤트 추가(#158/34), 인계(50), 박승철님 분석·Sandbox Workflow 내부 실패 이벤트, 승인 Workflow가 강제 종료된 경우의 표식 정리(운영 검사 경로, 후속)

## 변경 경계 (병렬 티켓과 충돌 최소화)
- `apps/worker/src/cmp_worker/workflows/migration.py`: `run`은 실패 감싸기만 하고 기존 본문은 `_run`으로 이름만 옮겼다. 추가 함수 `_record_failure`·`_report_failure`, `phase` property. `_approve_and_execute`에는 `failure_issues` 한 줄만 더했다. 44·50은 `_run`·`_hand_off`·`_plan_and_provision`의 시작·인계 부분을 고친다.
- `apps/api/src/cmp_api/routes/migrations.py`: `get_migration`·`read_workflow_status` 본문만 바꿨다(새 모듈 `cmp_api.migration_state` 호출). `start_migration`은 그대로다.

## 수용 기준 (Issue #184)
- [x] 로컬 통합 실행에서 Plan 실패 시 웹이 실패 단계와 코드를 받는다. (데모 compose 로컬 실측 R1·R3)
- [x] 실패·종료된 Workflow가 running으로 남지 않는다. (테스트 서버 + 로컬 실측 terminate)

## Test Seam
- Workflow: Temporal time-skipping 테스트 서버 + 실제 `MigrationWorkflow`·승인 Workflow + 가짜 Provisioning Activity + 실제 `emit_migration_event`(`InMemoryEmitter`). 재생(Replayer) 포함.
- 상태: 같은 테스트 서버에서 실제 Workflow를 실패·종료(terminate)시킨 뒤 `cmp_api.migration_state.read_migration_state`.
- SSE: 기존 `tests/test_event_stream.py` 방식(FastAPI `TestClient` + SQLite + 가짜 상태 조회).
- 실행 표식: SQLite + `claim_execution`, 승인 Workflow + complete 일시 실패 가짜 Activity.
- 놓치는 것: 실제 PostgreSQL 동시성, 실제 데모 서버.

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| F1 error 이벤트 | 실패 종료(`failed`·`plan_failed`·`provisioning_failed`·`plan_rejected`·`approval_expired`, 처리되지 않은 예외)마다 `emit_migration_event` Activity로 `type=error`, `status=failed`, `phase=실패 단계`, `data={code, message:"", phase, retryable:false}`를 1건 낸다. 멱등 키 `failure:<code>`. 취소는 내지 않는다(SSE `cancelled`로 충분) | 웹 계약 `ErrorData`는 code·message·phase·retryable만 받는다(`additionalProperties:false`, 웹 저장소 수정 금지). E3에 따라 문구는 비운다 |
| F2 code | `code`는 Workflow 실패 코드다. Plan 게시 실패는 게시 결과 `code`(`PLAN_FAILED`·`PLANNING_INPUT_MISSING` 등), 승인·실행 실패는 승인 결과 `code`, 인계·Sandbox 실패는 기존 `failure_code`, 예외는 `ACTIVITY_FAILED`·`CHILD_WORKFLOW_FAILED`·`WORKFLOW_FAILED`. 예외 메시지는 비밀이 섞일 수 있어 싣지 않는다 | 화면은 code로 문구를 고른다 |
| F3 issues | 세부 사유 코드(`issues`)는 웹 `error`에 자리가 없어 (1) Workflow `failure_issues`·상태 API, (2) `migration_logs` `MIGRATION_FAILED` 로그 `fields.issues`에 둔다. 웹 `ErrorData`에 `issues?: string[]`를 추가할지는 웹 담당 협의 | 계약을 깨지 않고 원인을 운영자·에이전트가 읽을 수 있게 한다 |
| F4 실패 단계 | `phase` property가 마지막 진행 phase를 기억한다. 웹 phase = 공통 대응표(`cmp_contracts.migration.WEB_PHASE`). 결과별 예외: `plan_failed`→`planning`, `provisioning_failed`→`provisioning`, `plan_rejected`·`approval_expired`→`awaiting_approval` | 부모 phase는 실행 중에도 `awaiting_approval`이라(N16) 결과로 보정한다 |
| F5 결과 보존 | Workflow 반환값은 문자열 그대로 둔다. 실패 단계·코드·사유는 `status` query(종료 뒤에도 재생으로 조회 가능)와 이벤트·로그 저장소에 남긴다 | 반환 형식 변경은 호출자(스크립트·44·50)를 깨뜨린다 |
| F6 재생 | 실패 보고 Activity는 `workflow.patched("failure-event-v1")` 뒤에서만 실행한다. 처리되지 않은 예외는 보고 뒤 그대로 다시 던져 Temporal 실행 상태는 Failed로 남는다 | 기존 이력 재생 보존 |
| F7 발행 위치·실패 격리 | `emit_migration_event`를 Workflow worker(`migration` 큐)에도 등록하고 보고 Activity를 그 큐에서 실행한다. start-to-close 30초·schedule-to-close 2분·최대 3회. 실패해도 Workflow 결과를 바꾸지 않는다(E9) | Workflow가 도는 곳에는 항상 이 worker가 있다. Provisioning worker가 없거나 멈춘 환경(분석·Sandbox 테스트 포함)에서 실패 보고가 2분씩 막히지 않는다. DB 쓰기뿐이라 Workflow 큐에 Provider I/O가 생기지 않는다 |
| F8 상태 API | `cmp_api.migration_state.read_migration_state`: `describe()`로 실행 상태를 먼저 본다. RUNNING이면 query 그대로. 종료면 query(5초 제한)를 시도하고 실패해도 describe만으로 상태를 만든다. FAILED·TERMINATED·TIMED_OUT인데 phase가 진행 phase면 `phase=failed`, `failure_code=WORKFLOW_<상태>`(이미 있으면 유지), `failure_phase=마지막 phase의 웹 phase`. CANCELED는 `cancelled`. 응답에 `execution_status` 추가. 응답 모델 `MigrationStatus`에 `plan`·`plan_code`·`failure_code`·`failure_phase`·`failure_issues`·`execution_status`를 선택 필드로 추가 | Terminated·TimedOut은 Workflow 코드가 돌지 않아 Workflow 안에서 기록할 수 없다 |
| F9 SSE | SSE도 같은 함수로 상태를 읽는다. phase 대응은 공통 표를 쓰고 `planning_handoff`·`workload_planning`→`planning`(N3). snapshot phase는 이벤트가 없으면 진행 phase, 실패면 `failure_phase` | 단일 대응표 |
| F10 claim 멱등 | 표식에 `claimed_by`(승인 Workflow `workflow_id/run_id`) 열을 추가한다(Alembic `20261106_01`). 같은 실행이 다시 claim하면 RUNNING 표식에 대해 `CLAIMED`를 다시 돌려준다. 다른 실행(재전달로 새로 시작한 승인 Workflow 등)은 그대로 `ALREADY_EXECUTED` | 같은 실행은 claim 응답을 받기 전이라 Provider를 시작하지 않았다. 다른 실행은 실행 여부를 모르므로 막아야 한다 |
| F11 complete 재시도 | complete Activity는 2분 start-to-close, 최대 간격 5분으로 6시간까지 재시도한다(완료된 표식은 다시 쓰지 않아 멱등). 그래도 실패하면 `workflow.patched("execution-record-v1")` 뒤에서 예외 대신 `{status: UNKNOWN, code: EXECUTION_RECORD_FAILED}`로 끝내 부모가 `provisioning_failed`와 코드를 받는다 | DB 일시 장애로 표식이 고착되지 않게 한다. 영구 실패면 표식 RUNNING은 "실행 여부 모름, 검사 필요"라는 원래 의미 그대로다 |

## 구현 위치
- 계약: `cmp_contracts.migration`(`WEB_PHASE`·`MigrationStatus` 확장)
- Workflow: `cmp_worker.workflows.migration`, `cmp_worker.workflows.approval`
- 발행: `cmp_worker.events.emit_migration_event`(선택 `log`)
- API: `cmp_api.migration_state`, `cmp_api.routes.migrations`(`get_migration`·`read_workflow_status`), `cmp_api.event_stream`
- 표식: `cmp_api.approvals`(`PlanExecution.claimed_by`, `claim_execution`), `cmp_worker.activities.approval`, Alembic `20261106_01_execution_claimant`

## 검증
- `tests/test_failure_visibility.py` 15건: Plan 실패 code·issues 보존과 `error` 1건, 처리되지 않은 Activity 실패(예외 메시지의 비밀 미보존, 실행 Failed 유지), 정상 scaffold는 `error` 없음, terminate → `failed`, worker 없는 종료 Workflow도 describe로 `failed`, N3 대응, snapshot phase, claim 재시도 멱등·Activity claimant, complete 일시 실패 4회 뒤 SUCCEEDED, 영구 실패 시 `EXECUTION_RECORD_FAILED`. 모든 Workflow 테스트는 Replayer 재생 포함.
- `make check`: ruff·format 통과, 전체 테스트 통과(PR 본문에 개수 기록). `make openapi` 재생성: `MigrationStatus` 선택 필드 6개와 `GET /migrations/{id}` 설명만 바뀜.
- Alembic(SQLite): `upgrade head` → `check`(변경 없음) → `downgrade 20261105_07` → `upgrade head`.
- 로컬 통합 실측(데모 `infra/demo/compose.yaml` + 점검 D 하네스 override, 별도 프로젝트·포트, 클라우드 자격 없음, 끝난 뒤 컨테이너·볼륨·이미지 삭제):
  - R1 웹과 같은 `POST /migrations {"project_id":"demo-1"}`: 상태 API `phase=plan_failed`, `failure_code=PLANNING_INPUT_MISSING`, `failure_phase=planning`. SSE snapshot `phase=planning`, `status=failed`, events에 `error{code:PLANNING_INPUT_MISSING, phase:planning}` 1건, 스트림이 닫힘. 운영 로그 `MIGRATION_FAILED` 1건.
  - R3 AWS Planning 입력 등록 후 시작: `plan_code=PLAN_FAILED`, `failure_issues=[MISSING_PROVIDER_LOCK]`(D-01 원인 코드가 이제 API에 보임), SSE `error` 1건, 로그 `fields.issues=[MISSING_PROVIDER_LOCK]`.
  - 진행 중 Workflow terminate: 상태 API `phase=failed`, `failure_code=WORKFLOW_TERMINATED`, `execution_status=TERMINATED`, SSE snapshot `status=failed`로 즉시 닫힘.
- 기존 테스트 조정: `tests/analysis/test_workflow.py`의 자식 실패 시 부모 phase 기대값을 `analysis` → `failed`(+`failure_phase=analysis`)로 바꿨다. 이 티켓이 고치려는 D-05 동작 자체다(박승철님 공유 필요).

## 미검증
- 데모 EC2(x86_64)·실제 PostgreSQL 동시성. `make smoke-temporal`은 별도로 돌리지 않았다(같은 Temporal 서버 경로를 로컬 compose 실측으로 확인).
- 강제 종료된 Workflow는 Workflow 코드가 돌지 않아 `error` 이벤트가 저장되지 않는다. 웹은 snapshot `status=failed`로 종료를 안다(API 쪽 이벤트 합성은 하지 않음, 후속 판단).
- 승인 Workflow가 강제 종료되면 실행 표식은 RUNNING으로 남는다("실행 여부 모름, 검사 필요"라는 원래 의미). 운영자 정리 경로는 범위 밖.
- N16(실행 중 부모 phase `awaiting_approval`)은 고치지 않았다. 실패 단계는 결과별 보정(F4)으로 맞춘다.

## 문서
- `specs/provisioning/migration-events.md`: E11 보강, E18~E21 추가
- `tickets/provisioning/24-migration-events.md`: 후속 링크
- `tickets/provisioning/18-onprem-plan-publish.md`: 실패 결과 보존 링크
- 후속: [50](50-handoff-contract.md)이 실패 결과 `traffic_blocked`(실패 단계 `traffic`)를 추가했고, `phase` setter가 `data_migration`을 지우던 #138 병합 문제를 고쳤다(50 H8).

## 후속 (D-19, 2026-11-06 재검사)
### 배경
재검사(`c9d565a`)에서 회귀가 나왔다. worker가 멈춘 동안(재배포·장애) 이미 끝난 실패 실행(Temporal COMPLETED인 `plan_failed`)을 조회하면 상태 API가 `phase=created`, `failure_code=null`을 돌려주고 SSE는 `running`으로 남아 닫히지 않았다.
- 원인: 닫힌 실행의 query가 실패하면 `state=None`이 되고, COMPLETED는 실패 상태 목록에 없어 기본값 `created`로 떨어졌다. SSE `RUNNING_PHASES`에는 `created`가 들어 있다.
- 같은 이유로 FAILED 실행은 Workflow가 보고한 코드(`CHILD_WORKFLOW_FAILED` 등) 대신 일반 코드 `WORKFLOW_FAILED`로 나왔다.
- 참고(N34): 실행 중 Workflow를 worker 없이 조회하면 상태 API가 30초 뒤 503을 내고, SSE는 15초 동안 아무것도 보내지 않았다.

### 결정
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| F12 닫힌 실행의 결과 | query가 실패한 닫힌 실행은 Temporal과 이벤트 저장소가 이미 가진 값으로 판정한다. COMPLETED는 `handle.result()`(닫힌 실행은 worker 없이 즉시 반환) 문자열을 phase로 쓴다. 그 phase가 실패 결과이거나 Temporal 실행이 실패면, 저장된 마지막 `error` 이벤트(`migration_events`)의 code·phase를 쓴다. 없으면 결과 이름 대문자·`OUTCOME_PHASE`, 또는 `WORKFLOW_<상태>`를 쓴다. 결과를 읽을 수 없으면 `phase=unknown` | 끝난 실행의 결과는 이력에 있다. worker 유무에 따라 판정이 바뀌면 안 된다 |
| F13 SSE 판정 | `execution_status`가 RUNNING이 아닌데 done·cancelled가 아니면, phase와 상관없이 `failed`로 닫는다 | 어떤 기본값이 새어 들어와도 닫힌 실행이 `running`·`waiting`으로 보이지 않게 막는 마지막 방어선이다 |
| F14 실행 중 + worker 없음 (N34 일부) | 실행 중 Workflow의 query에도 5초 제한을 둔다. 상태 API는 그대로 503(상태를 모름)이다. SSE는 5초 안에 저장 이벤트로 판단해 snapshot을 보낸다 | 실행 중인 phase는 worker 없이 알 수 없어 지어내지 않는다. 30초·15초 무응답만 줄인다. 응답 계약은 바꾸지 않는다 |

### 변경 위치
`cmp_api.migration_state`(`read_migration_state(handle, *, last_error=)`, `_recorded_outcome`, `closed_state`, `stored_last_error`), `cmp_api.event_stream.migration_status`, `cmp_api.routes.migrations`(`get_migration`·`read_workflow_status`의 호출 인자만). Workflow 파일은 고치지 않았다(44·50과 겹치지 않음).

### 검증
- `tests/test_closed_run_state.py` 6건(RED 6건 → GREEN). 실제 worker로 끝낸 뒤 모든 worker를 멈추고 확인했다:
  - plan_failed(저장 이벤트 있음·없음)
  - scaffold_complete → done
  - Temporal FAILED → 보고한 `ACTIVITY_FAILED` 유지
  - 닫힌 실행은 phase와 상관없이 running 아님
  - 실행 중 + worker 없음 → 10초 안에 오류
- `make check`: 2012 passed, 9 skipped. OpenAPI는 바뀌지 않았다.
- 로컬 compose 실측: 데모 compose + 점검 D 하네스를 쓰고, 끝난 뒤 컨테이너·볼륨·이미지를 지웠다. R1(`plan_failed`) 뒤 `docker compose stop worker`를 하고 확인했다.
  - 상태 API: `phase=plan_failed`, `failure_code=PLANNING_INPUT_MISSING`, `failure_phase=planning`, `execution_status=COMPLETED`. query 제한 때문에 응답에 약 4초가 걸린다.
  - SSE: snapshot `status=failed`, `phase=planning`이고 약 4초 뒤 닫혔다.
  - worker 없이 실행 중인 Workflow: 상태 API는 5.0초에 503(이전 30초), SSE 첫 snapshot은 5.1초(이전 15초 무응답).
- 남은 것: worker가 없을 때 응답에 query 제한(5초)만큼 시간이 더 걸린다. 응답 지연을 없애려면 describe 결과를 캐시하는 등 별도 설계가 필요하다(이 후속의 범위 밖).

## 완료 체크리스트
- [x] RED → GREEN 테스트
- [x] `make check`, `make openapi`, Alembic(SQLite) upgrade/check
- [x] Temporal 테스트 서버 통합 테스트·재생
- [x] 로컬 compose 실측
- [x] SDD 문서 갱신(migration-events E11·E17~E20, plan §12, temporal-flow, events-guide, 18·24 후속)
- [ ] 조율 세션 리뷰
