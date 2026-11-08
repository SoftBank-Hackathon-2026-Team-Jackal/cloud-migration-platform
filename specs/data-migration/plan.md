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
       2. run_network_preflight  `network_preflight` 입력이 있으면(웹 ON_PREM 기본) 터널 뒤 MNP 검사 →
                           may_start_data_migration(같은 입력 PASS만). 아니면 BLOCKED(NETWORK_PREFLIGHT_*)
       3. dm_precheck      원본 precheck SQL → evaluate_precheck → data.phase(preflight|blocked)
       4. dm_copy_schema   pg_dump --schema-only 원본 → 대상(대상 앱이 먼저 만든 같은 컬럼의 표·소유 시퀀스는 빼고 복원, 컬럼이 다르면 TARGET_SCHEMA_PARTIAL), 전환 확인용 marker 테이블
       5. dm_subscribe     원본 publication, 대상 subscription(copy_data=true). 이름은 replication_names
       6. dm_sample (반복)  대상 pg_subscription_rel + 원본 슬롯 지연 → sync_events → data.*
                           streaming이 될 때까지 Workflow 타이머로 간격을 둔다(무기한 Activity 없음)
       7. dm_readiness (반복) 원본 신호 → readiness_event → data.readiness
       8. dm_switchover    PoC fast 순서, PgBouncer PAUSE·연결 대상 변경(ticket 04)
       9. dm_verify        테이블별 행 수·체크섬 → data.verify / cutover.verify
       끝. dm_tunnel_cleanup 터널을 만든 실행은 done·failed·cancelled·blocked 어느 끝이든 슬롯 정리 뒤
                           한 번(롤백이 터널을 쓰므로 스트리밍 중엔 안 지움, Provisioning 76)
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
- 감시 중 지연이 계속 늘거나 구독이 꺼지면 운영 로그를 남기고 사람의 판단을 기다린다(자동 삭제 없음). 원본 슬롯은 WAL을 붙잡으므로 취소 시 정리(구독·슬롯 삭제)를 별도 Activity로 둔다. 구독을 만든 뒤 복제가 실패해도 같은 정리를 시도하고, 정리 실패는 결과 `cleanup_code`로만 남긴다(Provisioning 72).
- 전환 중 marker 미도착·체크섬 불일치: 쓰기를 원래 주체로 재개하고 전환하지 않는다(PoC와 같음).
- 전환 뒤 롤백: 역방향 복제로 같은 절차를 반대로 실행한다. 트래픽 롤백과는 별개다(ADR-006 한계).

## ON_PREM 복제 경로 (2안, ADR-007)

NAT 뒤 K3s 대상은 [ADR-007](../../docs/decisions/ADR-007.md)을 따른다. `dm_tunnel`은 같은 게이트웨이 매니페스트를 쓰되 입구 Service(UDP LoadBalancer)를 **원본 클러스터**에 두고, 허용 대역을 대상 공인 IP로 둔다. 대상 게이트웨이가 `PEER_ENDPOINT`(원본 NLB 주소)로 접속한다. Worker의 대상 접근은 `target_kubeconfig_ref`(Agent 터널 + 중계 서버 경유, 좁힌 ServiceAccount)다.

| 단계 | 내용 | 담당·티켓 |
| --- | --- | --- |
| 1 | PoC: 원본 EKS UDP NLB ↔ 맥 k3d handshake·터널 DB 조회 실측 완료(전체 복제·전환은 후속) | Provisioning 69 |
| 2 | Agent가 k3d API 포트를 터널에 노출 | Provisioning |
| 3 | 게이트웨이 입구 방향 옵션(`tunnel.py`) | 티켓 67 |
| 4 | 중계 서버 TCP 전달(worker → k3d API) | 트래픽 담당 확인 |
| 5 | k3d kubeconfig용 ServiceAccount | Provisioning |
| 6 | 원본 쪽 수정 5개(compose `DATA_MIGRATION_AWS_PROFILE`, marker 표 비교 제외, cutover 이미지 빌드, 실패 시 슬롯 정리, 체크섬 UTC) | 데이터 담당 확인 |
| 7 | 웹 시작 입력 연결 | Provisioning |
| 8 | 웹 화면·report | Provisioning |

## 입구 방향 옵션 구현 (#297, Provisioning 67)

- 입력: `WireGuardTunnel.ingress: "target" | "source" = "target"`. target는 `source_egress_cidrs`, source는 `target_egress_cidrs`가 필요하다. 반대편 CIDR 필드는 생략/null만 가능하다. `standing_ingress: bool = false`는 `source` 입구에서만 허용한다(Provisioning 82: 고정 원본 NLB 재사용, 정리 시 보존). 빈 목록·동시 지정·IPv6·/16보다 넓은 범위·호스트 비트가 있는 CIDR·9개 이상은 거절한다.
- 원본 입구 Service: `loadBalancerClass: service.k8s.aws/nlb`; `service.beta.kubernetes.io/aws-load-balancer-` 접두사의 annotation은 `scheme=internet-facing`, `nlb-target-type=ip`, `attributes=load_balancing.cross_zone.enabled=true`, `healthcheck-protocol=tcp`, `healthcheck-port=8080`이다. in-tree `type: nlb` annotation은 쓰지 않는다. 헬스 포트는 원본 Pod에만 열고 공개 Service에는 UDP 51820만 둔다.
- 원본 입구 모드에서 대상 게이트웨이는 `PEER_ENDPOINT=<NLB DNS>:51820`, keepalive 25를 쓴다. 기본 target 입구의 15초 keepalive는 보존한다. 양쪽 DB 프록시 포트(5432), Secret 키 `private`, 리소스 이름 및 대상 DB NetworkPolicy는 그대로다.
- Activity 순서: 양쪽 Namespace·키 준비 → 선택한 입구의 게이트웨이·LB → **같은 클러스터**의 주소 대기 → 반대편 게이트웨이 → rollout → **접속하는 쪽**에서 handshake 확인. 대상 DB 허용 정책은 항상 대상 게이트웨이와 함께 적용한다. 재시도는 기존 Secret의 키를 재사용한다.
- `endpoint_timeout_s=600`, `handshake_timeout_s=180` 기본값은 유지한다. source 입구는 NLB DNS가 즉시 생기고 대상 healthy가 약 150초 뒤인 실측을 반영하여 handshake 마감 시각을 `max(주소 대기 시작 + endpoint_timeout_s, rollout 완료 + handshake_timeout_s)`로 둔다. AWS 상태 조회 권한을 추가하지 않고 실제 handshake로 확인한다. 주소/handshake 실패 코드는 기존 retryable 코드를 유지한다. source 입구 Workflow Activity 제한은 3600초로, 설정 상한(주소 1800·handshake 900)과 양쪽 rollout 대기까지 수용한다. target 입구는 기존 1800초다.
- `dm_tunnel_cleanup({request, tunnel})`: `dm_tunnel`과 같은 입력을 migration Activity worker에서 받는다. 반환은 `{name, cleaned: true}`, 명령 실패는 retryable `TUNNEL_CLEANUP_FAILED`다. Service를 먼저 `--wait=true --timeout=300s --ignore-not-found`로 삭제하여 LBC finalizer를 기다린다. 실패하면 나머지 리소스를 보존한다. 성공 뒤 양쪽 Deployment·peer Service·ConfigMap·Secret, 대상 DB NetworkPolicy를 이름으로 삭제한다. 공유 Namespace·다른 이전·DB는 보존한다.
- 정리는 호출자가 복제/역방향 복제와 PgBouncer의 터널 사용 종료를 확인한 뒤 명시적으로 실행한다. 자동 Workflow 호출은 하지 않는다. 호출 Activity의 전체 제한은 개별 삭제 대기(최대 10개 × 330초)를 고려해 3600초 이상으로 두며, heartbeat timeout을 지정한다면 단일 삭제 대기보다 길어야 한다. 실패 시 남은 자원에 대해 같은 입력으로 재시도할 수 있다. #72 슬롯 정리·자동 호출 정책은 별도 작업이다.
- [PoC 69 결과](../../docs/engineering/onprem-data-tunnel-poc.md)는 방향과 NAT keepalive의 선행 근거다. 제품 Activity, cross-zone 활성화, 전체 DB 복제·전환·롤백, AWS 자원 삭제 실측은 아직 별도 검증이 필요하다.

## Verification

- ticket 01: `uv run pytest tests/data`, `make check`
- ticket 02~04: 실제 EKS 원본(`cmp-demo-source`, ns `chat-source`)과 대상 클러스터, 부하 중 측정(PoC 기준: 잃은 쓰기 0건, 체크섬 일치)
- 웹: `NEXT_PUBLIC_API_MODE=sse`로 실제 이벤트 수신 확인(Q4 해결 후)
