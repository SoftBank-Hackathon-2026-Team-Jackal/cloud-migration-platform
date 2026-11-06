# Ticket 62 — 이전 결과 보고 API (`GET /migrations/{id}/report`)

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰 대기) — [Issue #235](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/235). 2026-11-06 조율 세션 지시로 착수(본선 데모 웹 실제 모드 연결). 담당 민서.

## 배경
웹 완료 화면(`cloud-migration-web` `src/components/MigrationReport.tsx`)은 `sseProvider.loadReport()`로 `GET /migrations/{id}/report`를 부른다. API가 없어 실제 모드에서 완료 화면이 `report_failed`로 끝났다. 샌드박스 화면의 `POST /migrations/{id}/sandbox/retry`도 API가 없다.

## 관련 요구사항
PROV-021(비밀값 비노출). 이벤트 저장소: [migration-events.md](../../specs/provisioning/migration-events.md)(티켓 24).

## 선행 작업
24(이벤트 저장소·SSE). 실제 값은 각 단계 이벤트 발행(58 분석, 59 샌드박스, 60 계획·승인, 61 트래픽)이 채운다.

## 작업 범위
- 포함: `GET /migrations/{id}/report`(새 파일 `apps/api/src/cmp_api/routes/migration_report.py`, `main.py` 등록)
- 제외: `POST /migrations/{id}/sandbox/retry`(아래 R2), `routes/migrations.py`·worker·`infra/demo/*` 변경, DB 스키마 변경(저장된 이벤트만 읽는다)

## 착수 결정 (2026-11-06, Issue #235 표)

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| R1 | report | 구현한다. 저장된 이전 이벤트(`migration_events`)로 단계별 시작·끝·소요 시간과 결과 요약을 계산한다 | 이벤트 저장소가 단계별 시각을 이미 가진다. 새 저장이 필요 없다 |
| R2 | sandbox/retry | **구현하지 않는다.** 웹에서 버튼을 숨긴다(남은 일) | 샌드박스는 이미 최대 3회 자동 재시도한다. 사람이 다시 돌리려면 `MigrationWorkflow`가 끝난 샌드박스 자식 Workflow를 다시 시작하는 구조 변경이 필요해 본선 범위 밖이다 |
| R3 | 라우트 위치 | 새 파일 `routes/migration_report.py`. 의존성(`get_event_sessions`, `get_status_reader`)은 `routes/migrations.py`의 것을 그대로 쓴다 | 다른 세션과 `migrations.py` 충돌을 피한다. 테스트 override가 SSE와 같다 |
| R4 | 단계 | 웹 레일 순서와 이름: `analysis`(analysis) · `rehearsal`(build) · `planning`(planning) · `approval`(awaiting_approval) · `provisioning`(provisioning) · `data`(data_copy) · `traffic`(traffic) · `cutover`(verification). 괄호는 이벤트 `phase` | 웹 `src/lib/stages.ts`의 stage ↔ backendPhase 표와 같다 |
| R5 | 시각 | 단계 `started_at`=그 phase 첫 이벤트 `at`, `ended_at`=마지막 이벤트 `at`, `seconds`=정수 초(반올림). `total_seconds`=첫 단계 시작 → 마지막으로 채운 단계 끝(벽시계, 단계 사이 공백 포함) | phase가 시간상 겹쳐도(예: 데이터 복제 중 트래픽 준비) 단계마다 자기 이벤트 범위만 잰다. 웹 `clock()`은 정수 초를 가정한다 |
| R6 | 진행 중 | 끝난 단계만 채운다. 끝남 = 상태가 done·failed·cancelled이거나, 레일 순서상 뒤 단계에 이벤트가 있음. 이벤트가 있는 마지막 단계는 `current_stage`로만 알린다 | 이슈 범위 "진행 중이면 끝난 단계까지만". 뒤 단계 이벤트가 앞 단계 중에 먼저 오면 앞 단계를 끝난 것으로 본다(알려진 한계) |
| R7 | 상태 | SSE와 같은 `migration_status`(Workflow 조회 우선, 조회 불가면 마지막 이벤트). 실패 단계는 그 phase에 `failed` 이벤트나 `error`가 있으면 `status=failed` | E11·E19와 같은 판정 |
| R8 | 없는 이전 | Temporal이 Workflow 없음이라고 답하고 저장 이벤트도 없으면 404 `{"detail":{"code":"MIGRATION_NOT_FOUND"}}`. Temporal 조회 불가면 이벤트로 답한다(빈 이벤트면 `status=idle`, 빈 단계) | `GET /migrations/{id}/events`와 같은 규칙. detail은 코드만 |
| R9 | `ai_fixes` | `build.fix`의 `id`와 `sandbox.attempt.fix_ids`의 합집합 개수 | 웹 reducer가 `build.fix`를 id로 upsert한다. 샌드박스만 fix_ids를 낼 수 있어 합친다 |
| R10 | 결과 요약 `results` | 코드·개수·식별자만: `findings`(analysis.finding id 수), `sandbox{status, attempts}`, `build{status, reused}`, `approval`(마지막 결정), `resources{done, failed}`(provision.resource id별 마지막 상태), `data_verify[{name,state,source,target}]`, `cutover[{id,value,ok}]`, `error{code,phase}` | 자유 문자열(`message`·`detail`·`why`·`evidence`·`lines`)은 읽지 않는다. 저장 시 마스킹(E4·E5)에 더해 이중 방어 |
| R11 | 인증 | 기존 `/migrations` 경로와 같다(라우터 수준 인증 없음, 데모 에지가 앞단을 맡는다) | 조율 세션 지시 |

## 응답 예시

```json
{
  "migration_id": "migration-…",
  "status": "running",
  "current_stage": "provisioning",
  "stages": [
    {"id": "analysis", "phase": "analysis", "status": "done",
     "started_at": "2026-11-07T01:00:00.000Z", "ended_at": "2026-11-07T01:00:42.000Z", "seconds": 42},
    {"id": "rehearsal", "phase": "build", "status": "done", "started_at": "…", "ended_at": "…", "seconds": 18}
  ],
  "total_seconds": 110,
  "ai_fixes": 2,
  "results": {"findings": 1, "sandbox": {"status": "PASSED", "attempts": 2}, "build": null,
              "approval": "APPROVE", "resources": {"done": 0, "failed": 0}, "data_verify": [],
              "cutover": [], "error": null}
}
```

## 웹과의 차이
- 웹 `ReportData`는 `{stages: [{id, seconds}], ai_fixes}`만 쓴다. 이 API는 그 필드를 같은 이름·같은 id로 주고, `migration_id`·`status`·`current_stage`·`stages[].phase/status/started_at/ended_at`·`total_seconds`·`results`를 더 준다(웹은 무시).
- 웹 총합은 `stages[].seconds`의 합이다. `total_seconds`는 벽시계라 단계 사이 공백만큼 클 수 있다.
- 진행 중·실패면 `stages`가 8개보다 적다. 웹은 받은 단계만 그린다.

## 수용 기준
- [x] 없는 migration 404(코드만)
- [x] 진행 중: 끝난 단계만, `current_stage`
- [x] 승인 대기: 승인 단계 열림
- [x] 완료: 8단계 웹 순서·결과 요약, Temporal 없이 마지막 이벤트로 완료 판정
- [x] 실패: 실패 단계 `failed`, 오류 코드만(메시지 없음)
- [x] 비밀값 비노출: 자유 문자열 필드를 복사하지 않음

## 검증 기록
- `tests/test_migration_report.py`(7개): FastAPI `TestClient` + SQLite + `DbEventEmitter`(고정 시계) + 가짜 상태 조회
- `make check`, alembic(SQLite) upgrade·check(스키마 변경 없음), `make openapi`

## 남은 일
- 웹: 샌드박스 화면의 다시 실행 버튼(`retrySandbox` → `POST /sandbox/retry`)을 실제 모드에서 숨긴다(R2).
- 웹: 완료 화면 총합에 `total_seconds`를 쓸지 결정(선택).
- 실제 값은 58~61 이벤트 발행이 연결된 뒤 데모 서버에서 확인한다.
