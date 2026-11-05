# Ticket 05 — DataMigrationWorkflow와 copy_data 교체, Traffic DB 게이트

## 상태

자식 Workflow: DONE(로컬 Temporal + k3d 검증, PR 전). 부모 `MigrationWorkflow` 연결: BLOCKED — 원본 DB 접근값(분석 결과)과 대상 인계값(`DATA_MIGRATION` 보기)을 `DbEndpoint`로 채우는 곳이 정해지지 않음. Traffic(#63)도 같은 이유로 부모 연결을 미뤘다.

## 구현

- `DataMigrationWorkflow`(`cmp_worker.workflows.data_migration`), 마이그레이션 Worker에 Workflow·Activity 10개 등록
- 흐름: 사전 점검(막히면 `blocked`, 아무것도 만들지 않음) → 준비 → 스키마 → 구독 → streaming까지 Workflow 타이머로 감시 → PgBouncer 설치·앱 연결 → 감시하며 신호 대기
- 신호: `switchover`(원본이 쓰기 주체일 때만), `rollback`(대상이 쓰기 주체일 때만), `finish`, `request_cancel`(전환 전이면 구독·발행 정리)
- 전환은 한 번만 시도한다(재시도 없음). 거절되면 쓰기 주체를 그대로 두고 감시를 계속한다.
- Query: `status`, `db_gate`(`writer`, `reverse_replication_ok`) — Traffic `require_db_gate`가 읽는 값(DM-007)
- 감시 200회마다 continue-as-new로 이력 길이를 묶는다

## 시험

- 가짜 Activity + time-skipping 서버: 정상 흐름(전환→롤백→종료), 사전 점검 막힘, 전환 거절 후 취소 (`tests/data/test_data_workflow.py`)
- 실제 Activity + 로컬 Temporal 서버 + data-lab(초당 약 50건 쓰기 중): streaming 3.1초, 전환 6ms·롤백 5ms, 체크섬 일치, DB 게이트 source → target(역방향 정상) → source, 앱 오류 증가 0, 커밋된 쓰기 누락 0

## 남은 일

- 부모 `MigrationWorkflow`의 `data_copy` 단계에서 이 자식을 시작(인계값 형식 확정 후, `workflow.patched` 표식으로 기존 이력 유지)
- 두 번째 승인(Q5)을 `switchover` 신호 앞에 둘지 전환 담당과 결정
- ON_PREM이면 사전 점검 전에 MNP Preflight(`run_network_preflight`) 호출
