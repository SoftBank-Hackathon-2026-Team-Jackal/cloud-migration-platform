# Feature: Data Migration (PostgreSQL 논리 복제 CDC)

- 담당: 소울
- 상태: DRAFT (2026-11-05, 사용자 검토 전)
- 근거: 예선·본선 PoC `Softbank-Hackathon-2026-Finals/poc/cdc`(로컬 Docker, AWS EC2 ↔ GCP GCE WireGuard), `poc/demo`(앱 2개 + HAProxy + 제어판)

## Goal

원본 PostgreSQL의 데이터를 대상 PostgreSQL로 옮기고, 쓰기 중단 시간을 최소화하며 DB 쓰기 주체를 대상으로 전환한다. 전환 뒤에는 역방향 복제로 롤백할 수 있게 한다. 진행 상황은 웹 이벤트 계약(`docs/contracts/events.schema.json`)의 `data.*`·`cutover.*` 이벤트로 낸다.

## In scope / out of scope

포함:
- 원본 사전 점검(wal_level, 슬롯 여유, PK 없는 테이블, 복제되지 않는 객체)
- 스키마 복사 → publication·subscription 생성(초기 복사 + CDC) → 동기화 감시
- 전환 가능 시점 판단(readiness)과 DB 전환(쓰기 중단 → 마지막 변경 도착 확인 → 시퀀스 맞춤 → 역방향 복제 준비 → 쓰기 재개)
- 정합성 확인(테이블별 행 수·체크섬), 역방향 복제를 이용한 DB 롤백
- 웹 이벤트(`data.phase`·`data.lsn`·`data.table`·`data.readiness`·`data.verify`, `cutover.*`)

제외:
- 트래픽 가중치 전환(Traffic, ADR-006). Data Migration은 DB 게이트 값(쓰기 주체·역방향 복제 상태)만 제공한다.
- 객체 스토리지 이전(제품 정책상 P0 제외)
- 물리 복사(`pg_createsubscriber`, PG17 전용): 플랫폼 대상 DB가 PostgreSQL 16이라 쓰지 않는다.
- 네트워크 경로 Preflight 자체(MNP 명세). Data Migration은 ON_PREM 이전 시작 직전에 호출만 한다.

## Requirements

- DM-001 실행 위치: CDC 명령은 Worker가 kubeconfig로 원본·대상 DB Pod 안에서 `psql`을 실행하는 방식으로 한다(`kubectl exec`, Provisioning `database_probe`와 같은 방식). DB를 Worker에 노출하지 않는다. (결정 2026-11-05)
- DM-002 입력: Provisioning 인계값 `DATA_MIGRATION` 보기(`database` 섹션: host·port·name·user·password Secret 위치·`replication_capable`·`tunnel_endpoint`)와 원본 DB 접근 참조. 인계값이 `proceed=false`면 아무것도 실행하지 않고 막힌 이유를 낸다.
- DM-003 사전 점검: 막는 조건(버전 < 10, `wal_level != logical`, 빈 슬롯 없음)이 있으면 복제를 시작하지 않는다. PK 없는 테이블은 `REPLICA IDENTITY FULL` 조치 대상으로 기록한다. 대용량 객체·unlogged 테이블·확장은 경고로 남긴다.
- DM-004 멱등성: publication·subscription·slot 이름은 migration_id와 방향으로 고정한다. Activity 재시도는 존재 여부를 조회한 뒤 이어서 진행한다.
- DM-005 감시: 복제 상태는 짧은 Activity를 반복 호출해 읽는다(무기한 Activity 금지, temporal-flow §CDC 장기 실행). 단계는 `schema → copy → catchup → streaming`이다.
- DM-006 전환 가능 시점: 가장 긴 트랜잭션 ≤ 200ms, 잠금 대기 0건, 슬롯 지연 ≤ 1 MiB일 때만 쓰기를 멈춘다(PoC `readiness.py`).
- DM-007 DB 전환과 트래픽 전환은 분리한다. 전환 결과(쓰기 주체, 역방향 복제 상태)를 Traffic의 DB 게이트(`require_db_gate`)가 읽을 수 있게 남긴다.
- DM-008 비밀값: DB 비밀번호는 Kubernetes Secret에서만 읽고, Workflow 입력·결과·이벤트·로그에 남기지 않는다.
- DM-009 이벤트: 웹 계약을 지키는 값만 낸다(`cmp_contracts.events.validate_data` 통과). 웹 안내서(민서, Notion "이벤트 저장소·SSE·운영 로그"·`docs/engineering/events-guide.md`)가 Data에 요구하는 값: 사전 검사 4항목·시작 가능 여부, 단계, 원본·대상 LSN 원문·지연 바이트·지연 시간(1초), 테이블별 복사 행 수·**전체 행 수**·상태, 전환 가능 시점(1초), 행 수·체크섬 비교.
- DM-010 쓰기 중단 장치는 PgBouncer다(결정 2026-11-05). Data Migration이 원본 클러스터에 PgBouncer를 두고 원본 앱의 DB 연결을 PgBouncer로 바꾼다(최초 1회 앱 재시작). 전환은 PoC처럼 `PAUSE` → 마지막 변경 확인 → PgBouncer 연결 대상을 대상 DB로 변경 → `RESUME`이며, 앱 연결은 끊지 않는다. 롤백은 연결 대상을 원본으로 되돌린다.
- DM-014 전환 순간 옛 주 DB에 쓰는 클라이언트는 모두 PgBouncer를 거쳐야 한다. 직접 붙은 세션이 하나라도 있으면 전환 실행기가 거절한다(`DIRECT_CLIENTS`, 시작할 때와 PAUSE 직전). 앱 연결을 PgBouncer로 바꾼 뒤에는 rollout 완료뿐 아니라 옛 Pod가 모두 사라질 때까지 기다린다. 근거: EKS 시험에서 종료 중이던 옛 앱 Pod(SIGTERM 무시)가 전환 뒤 12초 동안 옛 주 DB에 직접 써서 커밋된 쓰기 594건이 새 주 DB에 없었다(롤백 검증 불일치로 발견).
- DM-012 대상 DB의 `wal_level`이 logical이 아니면(Provisioning 27(#151) 이후 플랫폼 렌더러는 기본으로 logical을 넣으므로, 이 처리는 그 이전 배포·외부 DB를 위한 예비 경로다) 준비 단계에서 `ALTER SYSTEM`으로 바꾸고 Pod를 한 번 재시작한다. 역방향 복제(롤백)의 발행자가 되기 위해서다. 이 시점의 대상 DB에는 사용자가 없다.
- DM-013 구독자는 전용 역할 `cmp_repl`(LOGIN REPLICATION, public 스키마 SELECT)로 발행 DB에 접속한다. 비밀번호는 Worker가 만들어 구독자 쪽 Secret(`cmp-<키>-<fwd|rev>`)에 두고 SQL은 stdin으로만 넘긴다.
- DM-011 복제 경로는 WireGuard다(결정 2026-11-05). 정방향은 대상 DB(구독자)가 원본 DB로, 역방향은 원본 DB가 대상 DB로 터널을 통해 접속한다. 전환 뒤 PgBouncer도 터널을 통해 대상 DB로 붙는다. AWS·GCP 대상의 터널 구성은 PoC 2단계(EC2 ↔ GCE, MTU 1420)와 Traffic 중계 서버 터널(ADR-006) 방식을 따른다. ON_PREM(NAT 뒤 K3s)은 같은 데이터 게이트웨이(`cmp_data.tunnel`)를 쓰되 **입구(UDP LoadBalancer)를 원본 클러스터 쪽에 두고 대상 게이트웨이가 밖으로 접속한다**(2안, [ADR-007](../../docs/decisions/ADR-007.md), 결정 2026-11-07). ON_PREM 입력은 `tunnel.ingress="source"`로 지정한다. 기본 `ingress="target"`은 기존 EKS→GKE 경로를 유지한다. `source` 입구에는 `target_egress_cidrs`(대상의 실제 송신 공인 IP), `target` 입구에는 `source_egress_cidrs`만 필수다. 반대 방향 CIDR을 함께 지정하면 거절하며, 허용 범위는 IPv4 `/16` 또는 더 좁은 CIDR 1~8개다. 입구는 그 대역의 UDP 51820만 받는다. 원본 입구는 AWS LBC의 공개 NLB(IP 대상, cross-zone 활성화)이며 TCP 8080으로 게이트웨이 상태를 확인한다. 대상은 NLB DNS에 keepalive 25초로 접속한다. `tunnel.standing_ingress=true`(`source` 입구만, Provisioning 82)면 원본 NLB Service를 고정 이름(`cmp-wg-standing-endpoint`)으로 재사용하고 실행마다 selector·허용 대역만 맞추며 정리 때 남긴다. 게이트웨이·키·정책은 실행마다 만들고 지운다. `peer_host`·DB 허용 정책·키 Secret 규칙은 방향과 관계없이 같다. Worker는 Agent 터널 + 중계 서버를 거친 kubeconfig(권한을 좁힌 ServiceAccount, `target_kubeconfig_ref`)로 대상 K3s API에 붙는다. 대안(1안, 이전 문구): 기존 Agent 터널(`tunnel_endpoint`, #71) + 중계 서버 + 원본 VPC 피어링으로 복제 데이터가 중계 서버를 지나는 경로. 고객 VPC 변경·플랫폼 인프라 경유 때문에 채택하지 않았다.

입구 옵션의 정리는 `dm_tunnel_cleanup` Activity로 명시적으로 요청한다. 복제·역방향 복제 및 전환 후 PgBouncer의 터널 사용이 모두 끝난 뒤 호출해야 한다. 입구 Service 삭제 완료(LBC finalizer)를 먼저 기다린 다음, 해당 이전의 양쪽 게이트웨이·peer Service·ConfigMap·키 Secret과 대상 DB 허용 정책을 삭제한다. 공유 Namespace는 보존한다. Workflow 완료·실패 시 자동 호출과 슬롯 정리 순서 통합은 이 옵션 작업(#297) 범위 밖이다.

## Acceptance criteria

- 판단 로직(사전 점검·단계·readiness·이름·이벤트)이 DB 없이 단위 시험된다. → ticket 01
- PgBouncer와 WireGuard 경로가 준비되고, 원본 앱이 PgBouncer를 거쳐 정상 동작한다. → ticket 06
- 실제 EKS 원본(chat-source)에서 대상 클러스터 DB로 초기 복사와 CDC가 동작하고, 웹에서 `data.*` 이벤트로 진행이 보인다. → ticket 02·03
- 부하 중 DB 전환의 쓰기 중단 시간, 잃은 쓰기 0건, 체크섬 일치를 기록한다. 롤백도 같은 기준을 만족한다. → ticket 04
- `MigrationWorkflow`의 `data_copy` 단계가 scaffold 대신 이 흐름을 실행한다. → ticket 05

## Open questions

- Q1 (해결 2026-11-05) 쓰기 중단 장치 → PgBouncer(DM-010).
- Q2 (해결 2026-11-05) 복제 경로 → WireGuard(DM-011). 남은 세부: AWS·GCP 대상에서 터널 양 끝을 노드·Pod·별도 VM 중 어디에 둘지, 터널 생명주기를 Provisioning과 Data 중 누가 소유할지(ticket 06).
- Q3 (해결 2026-11-05, 담당 판단) 원본·대상 DB 접근은 `DbEndpoint`(비밀값 없음)로 받는다: 접근 방식(`AWS_EKS` 이름·리전, `GCP_GKE` 프로젝트·이름·위치, `KUBECONFIG` 참조 이름), Namespace, Pod, 컨테이너, 상대가 접속할 주소(`peer_host`), DB 이름. 자격 증명은 Worker 설정(`DATA_MIGRATION_AWS_PROFILE`, `DATA_MIGRATION_GCLOUD_CONFIGURATION`, `DATA_MIGRATION_KUBECONFIG_DIR`)에만 있다. 원본 값은 분석 결과에서, 대상 값은 Provisioning `DATA_MIGRATION` 보기에서 채운다(ticket 05).
- Q4 (해결 2026-11-06) 웹 계약: 웹 저장소 main의 이벤트 정의·reducer가 `data.phase`·`data.lsn`·`data.table`·`data.readiness`를 처리한다. Data 쪽 이벤트는 `validate_data`로 같은 계약을 지킨다.
- Q5 (해결 2026-11-06) DB 전환 승인 → 웹이 정한 방식을 따른다. 자식이 `approval.required`(gate `cutover`)와 `cutover.plan`을 내고, 웹 승인 `POST /migrations/{id}/approve?gate=cutover`를 API가 자식 `switchover` 신호로 보낸다(ticket 07). 승인 기록(ADR-002)은 Plan 승인에만 쓴다.
