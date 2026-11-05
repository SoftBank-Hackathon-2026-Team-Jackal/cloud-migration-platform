# Ticket 03 — 동기화 감시와 data.* 이벤트 발행

## 상태

IN PROGRESS — `dm_sample`·`dm_readiness` Activity와 이벤트 발행은 구현·k3d 검증. 남은 일: 반복 호출(Workflow 타이머, ticket 05), 같은 값 연속 시 이벤트 줄이기, 웹 화면 확인(Q4)

## 목적

짧은 `dm_sample`·`dm_readiness` Activity를 Workflow 타이머 간격으로 반복해 복제 상태를 읽고, `cmp_data.sync_events`·`readiness_event` 값을 `emit_event_async`로 낸다(DM-005, DM-009).

## 포함 범위

- 대상 `pg_subscription_rel`(테이블 상태·행 수), 원본 슬롯 지연·`pg_current_wal_lsn()` 원문, 대상 수신 LSN 원문 조회(웹 안내서 요구, PoC 패널에는 없던 값)
- 테이블 전체 행 수: 원본 `pg_stat_user_tables.n_live_tup`(근사치)를 시작 시 한 번 읽어 `rows_total`에 넣는다
- 같은 값이 연속되면 이벤트를 줄인다(poc_bridge의 중복 제거와 같음)
- streaming 도달·제한 시간 초과·구독 꺼짐 판단

## 수용 기준(초안)

- 웹 SSE 모드에서 실제 마이그레이션의 data.phase·data.table·data.lsn·data.readiness가 보인다(Q4: 웹 저장소 반영 필요).
