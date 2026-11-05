# Ticket 05 — DataMigrationWorkflow와 copy_data 교체, Traffic DB 게이트

## 상태

DRAFT — 사용자 검토 전

## 목적

02~04 Activity를 `DataMigrationWorkflow`(자식)로 묶고 `MigrationWorkflow`의 `data_copy` scaffold를 교체한다. Traffic의 `require_db_gate`가 DB 전환 결과를 읽게 한다(DM-007).

## 포함 범위

- 자식 Workflow, 마이그레이션 Worker 등록, `workflow.patched` 표식으로 기존 이력 재생 유지
- 인계값(`DATA_MIGRATION` 보기) 읽기와 ON_PREM Preflight 호출
- 취소 시 구독·슬롯 정리 Activity

## 수용 기준(초안)

- 가짜 Activity로 Workflow 순서·BLOCKED·취소를 시험하고, `make smoke-temporal`을 통과한다.
