# Ticket 01 — CDC 판단 로직과 웹 이벤트 값

## 상태

DONE(PR #138 병합) — [Issue #137](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/137)

## 목적

PoC(`poc/cdc`) 스크립트의 판단 부분을 인프라 없이 시험할 수 있는 순수 모듈(`cmp_data`)로 옮기고, 그 값이 웹 이벤트 계약을 지키게 한다.

## 포함 범위

- 계약: `PrecheckFacts`·`PrecheckResult`·`ReadinessPolicy`·`ReadinessSample`·`SyncSample`·`TableSync`·`ReplicationNames`
- 사전 점검 `evaluate_precheck`(precheck.sql·시나리오 03), 복제 단계 `sync_phase`(03_wait_sync.sh), 전환 가능 시점 `evaluate_readiness`(readiness.py), 고정 이름 `replication_names`
- 웹 이벤트 값 `sync_events`(data.phase·data.lsn·data.table), `readiness_event`(data.readiness)

제외: SQL 조회·kubectl 실행(02), 이벤트 발행(03), 전환 절차(04).

## 시험 경계

`cmp_data` 공개 함수 + `cmp_contracts.events.validate_data`. 웹 계약에서 벗어난 값은 잡지만, 실제 카탈로그 SQL이 이 값을 맞게 채우는지는 잡지 못한다(02·03에서 실제 DB로 확인).

## 수용 기준

- [x] 막는 조건(버전, wal_level, 빈 슬롯)이 있으면 ok=false, PK 없는 테이블은 REPLICA IDENTITY FULL 조치, 복제되지 않는 객체는 경고.
- [x] 복제 단계가 schema → copy → catchup → streaming으로 PoC와 같은 기준에서 바뀐다. 지연을 모르면 streaming으로 보지 않는다.
- [x] readiness가 PoC 기준(200ms, 잠금 0, 1 MiB)으로 판단하고 이유를 코드로 준다.
- [x] 이름이 migration_id·방향마다 고정이고 PostgreSQL 식별자 규칙(63자, 소문자)을 지킨다.
- [x] 이벤트 값이 `validate_data`를 통과한다.

테스트: `tests/data/test_data_cdc_logic.py`.

## 관련 문서

[spec](../../specs/data-migration/spec.md) · [plan](../../specs/data-migration/plan.md) · [events-guide](../../docs/engineering/events-guide.md)
