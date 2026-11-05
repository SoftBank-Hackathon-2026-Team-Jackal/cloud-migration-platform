# Implementation plan: Data Migration

## Affected modules and contracts

| 위치 | 내용 |
| --- | --- |
| `packages/data/src/cmp_data` | 순수 판단 로직과 이벤트 값(ticket 01). 외부 I/O 없음 |
| `apps/worker/src/cmp_worker/activities/data_migration.py` (예정) | `kubectl exec … psql`로 SQL을 실행하는 Activity(ticket 02~04) |
| `apps/worker/src/cmp_worker/workflows/data_migration.py` (예정) | `DataMigrationWorkflow` 자식 Workflow(ticket 05). Traffic의 `TrafficShiftWorkflow`와 같은 구성 |
| `apps/worker/src/cmp_worker/activities/migration.py` `copy_data` | scaffold. ticket 05에서 자식 Workflow 호출로 교체 |
| `cmp_contracts.events` | 웹 이벤트 계약. 바꾸지 않고 `validate_data`로 검증만 한다 |
| `cmp_worker.poc_bridge` | PoC 값을 이벤트로 옮기는 임시 연결. 플랫폼 Activity가 같은 이벤트를 내면 쓰지 않는다 |

## Design and data flow

```text
MigrationWorkflow
  └─ data_copy 단계 → DataMigrationWorkflow (자식)
       1. dm_handover      Provisioning DATA_MIGRATION 보기 읽기(proceed=false면 BLOCKED)
       2. dm_preflight     ON_PREM이면 MNP Preflight 호출(PASS가 아니면 BLOCKED)
       3. dm_precheck      원본 precheck SQL → evaluate_precheck → data.phase(preflight|blocked)
       4. dm_copy_schema   pg_dump --schema-only 원본 → 대상, 전환 확인용 marker 테이블
       5. dm_subscribe     원본 publication, 대상 subscription(copy_data=true). 이름은 replication_names
       6. dm_sample (반복)  대상 pg_subscription_rel + 원본 슬롯 지연 → sync_events → data.*
                           streaming이 될 때까지 Workflow 타이머로 간격을 둔다(무기한 Activity 없음)
       7. dm_readiness (반복) 원본 신호 → readiness_event → data.readiness
       8. dm_switchover    PoC fast 순서(ticket 04, 쓰기 중단 장치 결정 후)
       9. dm_verify        테이블별 행 수·체크섬 → data.verify / cutover.verify
```

SQL 실행 경로: Worker → `kubectl --kubeconfig <참조> exec -n <ns> <db pod> -- psql -XAtc` (Provisioning `cluster.database_probe`와 같은 방식). 비밀번호는 Pod 안의 환경변수·Secret을 쓰고 명령줄에 넣지 않는다. 대상의 구독 연결 문자열에 들어가는 원본 비밀번호는 대상 클러스터 Secret에서 읽어 SQL을 Pod 안에서 조립한다(Q2·Q3 결정 후 구체화).

PoC 스크립트와의 대응:

| PoC | 플랫폼 |
| --- | --- |
| `00_precheck.sh`, `precheck.sql` | `dm_precheck` + `evaluate_precheck` |
| `01_copy_schema.sh` | `dm_copy_schema` |
| `02_start_replication.sh` | `dm_subscribe` |
| `03_wait_sync.sh` | `dm_sample` + `sync_phase` |
| `readiness.py` | `dm_readiness` + `evaluate_readiness` |
| `05_switchover_fast.py` | `dm_switchover` (ticket 04) |
| `04_compare.sh` | `dm_verify` |
| `06_rollback.sh` | `dm_rollback` (ticket 04) |

## Failure, retry, and rollback behavior

- 사전 점검 실패·인계값 미준비·Preflight 실패: 아무것도 만들지 않고 `BLOCKED`.
- 생성 Activity(publication·subscription)는 이름이 고정이라 재시도 시 존재 여부를 먼저 본다.
- 감시 중 지연이 계속 늘거나 구독이 꺼지면 운영 로그를 남기고 사람의 판단을 기다린다(자동 삭제 없음). 원본 슬롯은 WAL을 붙잡으므로 취소 시 정리(구독·슬롯 삭제)를 별도 Activity로 둔다.
- 전환 중 marker 미도착·체크섬 불일치: 쓰기를 원래 주체로 재개하고 전환하지 않는다(PoC와 같음).
- 전환 뒤 롤백: 역방향 복제로 같은 절차를 반대로 실행한다. 트래픽 롤백과는 별개다(ADR-006 한계).

## Verification

- ticket 01: `uv run pytest tests/data`, `make check`
- ticket 02~04: 실제 EKS 원본(`cmp-demo-source`, ns `chat-source`)과 대상 클러스터, 부하 중 측정(PoC 기준: 잃은 쓰기 0건, 체크섬 일치)
- 웹: `NEXT_PUBLIC_API_MODE=sse`로 실제 이벤트 수신 확인(Q4 해결 후)
