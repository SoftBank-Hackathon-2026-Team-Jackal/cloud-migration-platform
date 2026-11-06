# Tasks: Data Migration

| ID | 제목 | 상태 | Blocked by | Issue |
| --- | --- | --- | --- | --- |
| [01](../../tickets/data-migration/01-cdc-logic.md) | CDC 판단 로직과 웹 이벤트 값 | DONE(PR #138 병합) | - | #137 |
| [06](../../tickets/data-migration/06-pgbouncer-wireguard-path.md) | 원본 PgBouncer 배치와 WireGuard 복제 경로 | DONE(실습: k3d, EKS, EKS ↔ GKE) | - | #137 |
| [02](../../tickets/data-migration/02-kubectl-psql-activities.md) | kubectl exec psql 실행기와 사전 점검·스키마·구독 Activity | DONE(PR #138 병합) | 01 | #137 |
| [03](../../tickets/data-migration/03-sync-monitor.md) | 동기화 감시와 data.* 이벤트 발행 | DONE(로컬), 이벤트 줄이기·웹 화면 확인 남음 | 02 | - |
| [04](../../tickets/data-migration/04-switchover-rollback.md) | DB 전환·롤백과 정합성 확인 | DONE(PR #138 병합) | 03, 06 | #137 |
| [05](../../tickets/data-migration/05-workflow-wiring.md) | DataMigrationWorkflow와 copy_data 교체, Traffic DB 게이트 | DONE(로컬) | 03 | #137 |
| [07](../../tickets/data-migration/07-cutover-approval-events.md) | DB 전환 두 번째 승인과 웹 cutover 이벤트 | DONE(PR #215 병합), 웹 화면 순서 남음 | 04, 05 | #137 |
| [08](../../tickets/data-migration/08-web-start-data-migration.md) | 웹 실제 시작에 데이터 이전 입력 연결 | DONE(PR #254 병합, 로컬 웹 시작 e2e 통과), 데모 서버 값·worker 클러스터 접근 남음 | 05, 07 | #137 |
| [09](../../tickets/data-migration/09-verify-hide-marker.md) | 전환 검증 결과에서 내부 marker 테이블 제외 | DONE(PR #255 병합) | 07 | #137 |
| [10](../../tickets/data-migration/10-wireguard-tunnel.md) | 플랫폼이 만드는 WireGuard 복제 경로(EKS ↔ GKE) | DONE(PR #258 병합), 명시적 정리 Activity는 #297, 자동 호출 통합 남음 | 06, 08 | #137 |
| [11](../../tickets/data-migration/11-tunnel-cloud-check.md) | WireGuard 복제 경로 클라우드 검증(EKS ↔ GKE)과 게이트웨이 포트·GKE DNS 정책 수정 | DONE(클라우드 검증) | 10 | #137 |
| 12 | ON_PREM 복제 경로 2안: 게이트웨이 입구 방향 옵션([티켓 67](../../tickets/provisioning/67-data-tunnel-reverse-ingress.md), ADR-007 단계 3) | IN_REVIEW(로컬 PASS, 제품 실측 대기) | 10, 11, Provisioning 69 | #297 |
| 13 | 원본 쪽 수정 5개(compose `DATA_MIGRATION_AWS_PROFILE`, marker 표 비교 제외, cutover 이미지 빌드, 실패 시 슬롯 정리, 체크섬 UTC, ADR-007 단계 6) | TODO(데이터 담당 확인) | 12 | #288 |

Q1~Q5는 [spec.md](spec.md#open-questions)의 미결 항목이다.
