# Ticket 63 — 실제 모드 데이터 복제 이벤트 (data.progress·data.verify)

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰·실제 DB 복제에서 화면 확인 대기) — [Issue #236](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/236). 2026-11-06 조율 세션 지시로 착수. 담당 민서.

**데이터 범위 대행**: 데이터 이전은 데이터 담당 범위다. 본선(11/7~8) 일정 때문에 Provisioning이 대행하며, 복제·전환 동작(SQL, Workflow 흐름, 실행기)은 바꾸지 않고 발행만 더한다.

## 배경
웹 데이터 복제 화면·에이전트 문맥은 `data.phase`·`data.lsn`·`data.table`·`data.preflight`·`data.readiness`에 더해 `data.progress`(진행 값)·`data.verify`(정합성)를 읽는다. 두 이벤트는 계약(`cmp_contracts.events`)에 있지만 `data.progress`는 발행되지 않았고, `data.verify`는 #215(데이터 07)에서 전환 뒤 결과값만 냈다.

## 관련 요구사항
PROV-021(이벤트 계약), 데이터 03(동기화 감시)·07(전환 이벤트). 기준: 웹 `src/lib/migration/dataScript.ts`(`dataProgressFor`, `dataTickAt`)·`script.ts`(`DATA_VERIFY_TIMELINE`).

## 선행 작업
#215(데이터 07) 병합 뒤 origin/main에서 시작.

## 작업 범위
- 포함
  - `cmp_data.events.progress_event`: 복제 샘플 → `data.progress`. `dm_sample`이 같은 샘플로 낸다.
  - `cmp_data.cutover_events.verify_events`: `data.verify` 합계를 웹 순서(running 포함)로 낸다.
- 제외
  - 복제·전환 SQL·Workflow·실행기 변경, 새 DB 조회, 계약 변경, 웹 변경

## 착수 결정 (2026-11-06, Issue #236 표 보충)

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| D1 | 담당 | 데이터 담당 범위를 Provisioning이 대행. 동작은 그대로, 발행만 더한다 | Issue #236 |
| D2 | `data.progress` 위치 | `dm_sample`(동기화 감시, 복제 중 2초·감시 중 5초 주기)이 `data.phase`·`data.lsn`·`data.table` 다음에 한 번 낸다. 같은 샘플 값으로 계산해 DB 조회가 늘지 않는다 | 웹 틱도 lsn 다음 progress |
| D3 | 값 | `step`: schema·copy→`dump`, catchup→`cdc`, streaming→`ready`(`connect`는 샘플 전이라 안 나옴). `rows`·`total_rows` = 테이블별 `rows_copied`·`rows_total` 합. `dump_percent` = copy면 rows/total(0~100으로 자름, total 0이면 0), catchup·streaming이면 100. `lag_ms`는 샘플 값(지금 null). `writes_per_sec`는 샘플에 없어 0(실제 값은 `data.readiness.commits_per_sec`). 이벤트 status는 `ready`면 done, 아니면 running | 웹 `dataProgressFor`. `rows_total`은 원본 n_live_tup 근사치 |
| D4 | 바이트 | `data.progress` 계약에 바이트 필드가 없어 넣지 않는다. 바이트(슬롯 지연)는 이미 `data.lsn.lag_bytes`로 간다 | 계약 변경 없음 |
| D5 | `data.verify` 위치 | 새 검증을 만들지 않고, 기존 전환 뒤 스냅샷 비교(실행기 `verify` checksum·light) 지점(`dm_switchover` → `verify_events`)에서 낸다. 복제 ready 시점 행 수 검증은 하지 않는다 | 복제 중에는 정확한 비교가 없다(n_live_tup은 근사치, count(*)는 DB 부하). 웹도 "체크섬은 전환 때" |
| D6 | `data.verify` 순서 | `rows running → rows done(source·target 합) → checksum running → checksum done`. 이벤트 status는 checksum done만 done, 실패한 결과는 failed, 나머지 running. 비교 안 함(`verify=none`)이면 내지 않는다 | 웹 `DATA_VERIFY_TIMELINE`. 결과가 한 번에 오므로 running은 결과 직전에 함께 나간다 |
| D7 | 멱등 키 | progress: `progress:{step}:{rows}`(샘플마다 Activity 실행이 달라 actor로 구분, 같은 시도 재발행은 한 줄). verify: `{run}:{attempt}:{from}:rows:running`·`…:rows`·`…:checksum:running`·`…:checksum` | events-guide 규칙 |
| D8 | 비밀값 | 테이블 이름·행 수·상태만. DB 비밀번호·연결 문자열·주소 없음(시험으로 확인) | events-guide |

## 수용 기준
- [x] `data.progress`·`data.verify`가 계약(`validate_data`)을 통과한다.
- [x] 같은 시도 재발행이 중복되지 않고, 이벤트마다 멱등 키가 다르다.
- [x] 비밀번호·연결 주소가 이벤트에 없다.
- [x] 순서가 웹 대본과 같다(progress는 lsn·table 뒤, verify는 rows→checksum, running→결과).
- [ ] 실제 DB 복제에서 웹 실제 모드 화면 확인(본선 리허설).

## Test Seam
`tests/data/test_data_activities.py`(가짜 DbPod + `InMemoryEmitter`로 `dm_sample` 발행), `tests/data/test_data_cutover_events.py`(`run_events` 순수 함수), `tests/data/test_data_cutover.py`(`dm_switchover` 발행 순서).

## 검증
`make check`, `uv run alembic check`(스키마 변경 없음), `make openapi`(HTTP 변경 없음).

## 문서
`docs/engineering/events-guide.md` 데이터 표·"아직 없음" 갱신. 계약·API 변경 없음.
