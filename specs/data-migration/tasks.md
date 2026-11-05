# Tasks: Data Migration

| ID | 제목 | 상태 | Blocked by | Issue |
| --- | --- | --- | --- | --- |
| [01](../../tickets/data-migration/01-cdc-logic.md) | CDC 판단 로직과 웹 이벤트 값 | DONE(로컬, PR 전) | - | 미생성 |
| [06](../../tickets/data-migration/06-pgbouncer-wireguard-path.md) | 원본 PgBouncer 배치와 WireGuard 복제 경로 | DRAFT | - | - |
| [02](../../tickets/data-migration/02-kubectl-psql-activities.md) | kubectl exec psql 실행기와 사전 점검·스키마·구독 Activity | DRAFT | 01, 06, Q3 | - |
| [03](../../tickets/data-migration/03-sync-monitor.md) | 동기화 감시와 data.* 이벤트 발행 | DRAFT | 02 | - |
| [04](../../tickets/data-migration/04-switchover-rollback.md) | DB 전환·롤백과 정합성 확인 | DRAFT | 03, 06, Q5 | - |
| [05](../../tickets/data-migration/05-workflow-wiring.md) | DataMigrationWorkflow와 copy_data 교체, Traffic DB 게이트 | DRAFT | 03 | - |

Q1~Q5는 [spec.md](spec.md#open-questions)의 미결 항목이다.
