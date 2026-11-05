# Tasks: Data Migration

| ID | 제목 | 상태 | Blocked by | Issue |
| --- | --- | --- | --- | --- |
| [01](../../tickets/data-migration/01-cdc-logic.md) | CDC 판단 로직과 웹 이벤트 값 | DONE(로컬, PR 전) | - | #137 |
| [06](../../tickets/data-migration/06-pgbouncer-wireguard-path.md) | 원본 PgBouncer 배치와 WireGuard 복제 경로 | PgBouncer DONE(로컬), WireGuard TODO | - | - |
| [02](../../tickets/data-migration/02-kubectl-psql-activities.md) | kubectl exec psql 실행기와 사전 점검·스키마·구독 Activity | DONE(로컬 k3d, PR 전) | 01 | #137 |
| [03](../../tickets/data-migration/03-sync-monitor.md) | 동기화 감시와 data.* 이벤트 발행 | DONE(로컬), 이벤트 줄이기·웹 화면 확인 남음 | 02 | - |
| [04](../../tickets/data-migration/04-switchover-rollback.md) | DB 전환·롤백과 정합성 확인 | DONE(로컬 k3d, PR 전), Q5 남음 | 03, 06 | - |
| [05](../../tickets/data-migration/05-workflow-wiring.md) | DataMigrationWorkflow와 copy_data 교체, Traffic DB 게이트 | 자식 DONE(로컬), 부모 연결 BLOCKED(인계값) | 03 | - |

Q1~Q5는 [spec.md](spec.md#open-questions)의 미결 항목이다.
