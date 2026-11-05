# Ticket 05 — DataMigrationWorkflow와 copy_data 교체, Traffic DB 게이트

## 상태

DONE(로컬 검증, PR #138). 부모 연결은 `data-migration-v1` patch 아래에서 동작한다.

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

## 부모 연결 (`MigrationWorkflow` `data_copy` 단계)

- 조건: 시작 입력에 `data_migration`(`cmp_data.handoff.DataMigrationInput`)이 있고, Provisioning 실행이 `EXECUTED`이며, `data-migration-v1` patch가 적용된 새 실행. 아니면 기존 scaffold `copy_data`를 그대로 쓴다.
- 원본 DB·앱·실행기 이미지·대상 터널 주소(`target_peer_host`)는 운영자 입력이다(분석 결과에 원본 DB Pod 정보가 없음). 시작: `scripts/start_migration.py --data-migration '<JSON>'`
- 대상 `DbEndpoint`는 `get_migration_handoff`(consumer `DATA_MIGRATION`)로 만든다: EKS → `AWS_EKS`, GKE → `GCP_GKE`, DB Pod는 `<release>-0`. K3s는 `target_kubeconfig_ref`가 있어야 하고, 터널 DB 주소(`tunnel_endpoint`)를 기본 `peer_host`로 쓴다. 인계값이 막혀 있으면 `data_blocked`(이유 코드는 `failure_code`).
- 자식 `data-<migration_id>`를 `ABANDON`으로 시작한다(부모가 끝나도 역방향 복제를 유지해 롤백 가능). 자식은 단계가 바뀔 때 부모에 `data_migration_state` 신호를 보내고, 부모 `status` Query의 `data_migration`에 보인다.
- 부모는 DB가 대상으로 전환(`switched`)될 때까지 기다린 뒤 트래픽 단계로 간다(DB 먼저, DM-007). 실패·막힘이면 `data_failed`·`data_blocked`로 끝난다. 부모 취소는 자식에 `request_cancel`을 보낸다.
- Traffic DB 게이트(#128): 부모 `MigrationWorkflow.db_gate_ok()` — 쓰기 주체가 대상이고 역방향 복제가 streaming이면 True, 데이터 이전을 하지 않은 실행이면 None. 자식은 게이트 값이 바뀔 때마다 부모에 알린다(`data_migration_state`에 `writer`, `reverse_replication_ok`). 감시가 실패한 값(stale)으로는 True가 되지 않는다.
- 전환 신호: `temporal workflow signal --workflow-id data-<migration_id> --name switchover` (두 번째 승인 연결 전까지 운영자가 보낸다)

시험: `tests/data/test_data_parent.py`(인계값 → 대상 변환, 시험용 부모 + 진짜 자식: 전환 뒤에만 진행, 인계값 막힘)

## 남은 일

- 두 번째 승인(Q5)을 `switchover` 신호 앞에 둘지 전환 담당과 결정
- ON_PREM이면 사전 점검 전에 MNP Preflight(`run_network_preflight`) 호출
