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
       1b. dm_path         WireGuard 터널과 원본 PgBouncer 준비(ticket 06, 앱 연결 1회 변경)
       2. dm_preflight     ON_PREM이면 MNP Preflight 호출(PASS가 아니면 BLOCKED)
       3. dm_precheck      원본 precheck SQL → evaluate_precheck → data.phase(preflight|blocked)
       4. dm_copy_schema   pg_dump --schema-only 원본 → 대상, 전환 확인용 marker 테이블
       5. dm_subscribe     원본 publication, 대상 subscription(copy_data=true). 이름은 replication_names
       6. dm_sample (반복)  대상 pg_subscription_rel + 원본 슬롯 지연 → sync_events → data.*
                           streaming이 될 때까지 Workflow 타이머로 간격을 둔다(무기한 Activity 없음)
       7. dm_readiness (반복) 원본 신호 → readiness_event → data.readiness
       8. dm_switchover    PoC fast 순서, PgBouncer PAUSE·연결 대상 변경(ticket 04)
       9. dm_verify        테이블별 행 수·체크섬 → data.verify / cutover.verify
```

SQL 실행 경로: Worker → `kubectl --kubeconfig <참조> exec -n <ns> <db pod> -- psql -XAtc` (Provisioning `cluster.database_probe`와 같은 방식). 비밀번호는 Pod 안의 환경변수·Secret을 쓰고 명령줄에 넣지 않는다. 대상의 구독 연결 문자열(`host=<원본 터널 IP>`)에 들어가는 원본 비밀번호는 대상 클러스터 Secret에서 읽어 SQL을 Pod 안에서 조립한다(Q3 결정 후 구체화).

쓰기 중단: 원본 앱 Namespace의 `cmp-cutover` Pod(PgBouncer + 전환 실행기)를 쓴다. 전환은 밀리초 단위라 SQL마다 `kubectl exec`를 하지 않고, 실행기(`infra/data-migration/cutover/switchover.py`)가 Pod 안에서 접속을 유지한 채 한 번에 돈다. Activity는 stdin으로 설정(비밀번호 포함)을 넘기고 결과 JSON만 받는다. 연결 대상 변경은 PoC처럼 `databases.ini` 교체 + `RELOAD`이고, 성공하면 Secret의 연결 대상도 바꿔 Pod를 다시 만들어도 새 주 DB로 붙는다. 전환 뒤 PgBouncer는 터널(`peer_host`)을 통해 대상 DB로 붙는다.

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
| `poc/cdc/cloud`(WireGuard), compose `pgbouncer` | `dm_path` (ticket 06) |

## Failure, retry, and rollback behavior

- 사전 점검 실패·인계값 미준비·Preflight 실패: 아무것도 만들지 않고 `BLOCKED`.
- 생성 Activity(publication·subscription)는 이름이 고정이라 재시도 시 존재 여부를 먼저 본다.
- 감시 중 지연이 계속 늘거나 구독이 꺼지면 운영 로그를 남기고 사람의 판단을 기다린다(자동 삭제 없음). 원본 슬롯은 WAL을 붙잡으므로 취소 시 정리(구독·슬롯 삭제)를 별도 Activity로 둔다.
- 전환 중 marker 미도착·체크섬 불일치: 쓰기를 원래 주체로 재개하고 전환하지 않는다(PoC와 같음).
- 전환 뒤 롤백: 역방향 복제로 같은 절차를 반대로 실행한다. 트래픽 롤백과는 별개다(ADR-006 한계).

## ON_PREM 복제 경로 (2안, ADR-007)

NAT 뒤 K3s 대상은 [ADR-007](../../docs/decisions/ADR-007.md)을 따른다. `dm_path`는 같은 게이트웨이 매니페스트를 쓰되 입구 Service(UDP LoadBalancer)를 **원본 클러스터**에 두고, 허용 대역을 대상 공인 IP로 둔다. 대상 게이트웨이가 `PEER_ENDPOINT`(원본 NLB 주소)로 접속한다. Worker의 대상 접근은 `target_kubeconfig_ref`(Agent 터널 + 중계 서버 경유, 좁힌 ServiceAccount)다.

| 단계 | 내용 | 담당·티켓 |
| --- | --- | --- |
| 1 | PoC: 원본 EKS UDP NLB ↔ 맥 k3d 게이트웨이로 실제 복제·전환 | Provisioning 69 |
| 2 | Agent가 k3d API 포트를 터널에 노출 | Provisioning |
| 3 | 게이트웨이 입구 방향 옵션(`tunnel.py`) | 티켓 67 |
| 4 | 중계 서버 TCP 전달(worker → k3d API) | 트래픽 담당 확인 |
| 5 | k3d kubeconfig용 ServiceAccount | Provisioning |
| 6 | 원본 쪽 수정 5개(compose `DATA_MIGRATION_AWS_PROFILE`, marker 표 비교 제외, cutover 이미지 빌드, 실패 시 슬롯 정리, 체크섬 UTC) | 데이터 담당 확인 |
| 7 | 웹 시작 입력 연결 | Provisioning |
| 8 | 웹 화면·report | Provisioning |

## Verification

- ticket 01: `uv run pytest tests/data`, `make check`
- ticket 02~04: 실제 EKS 원본(`cmp-demo-source`, ns `chat-source`)과 대상 클러스터, 부하 중 측정(PoC 기준: 잃은 쓰기 0건, 체크섬 일치)
- 웹: `NEXT_PUBLIC_API_MODE=sse`로 실제 이벤트 수신 확인(Q4 해결 후)
