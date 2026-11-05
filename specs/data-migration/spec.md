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
- DM-009 이벤트: 웹 계약을 지키는 값만 낸다(`cmp_contracts.events.validate_data` 통과).

## Acceptance criteria

- 판단 로직(사전 점검·단계·readiness·이름·이벤트)이 DB 없이 단위 시험된다. → ticket 01
- 실제 EKS 원본(chat-source)에서 대상 클러스터 DB로 초기 복사와 CDC가 동작하고, 웹에서 `data.*` 이벤트로 진행이 보인다. → ticket 02·03
- 부하 중 DB 전환의 쓰기 중단 시간, 잃은 쓰기 0건, 체크섬 일치를 기록한다. 롤백도 같은 기준을 만족한다. → ticket 04
- `MigrationWorkflow`의 `data_copy` 단계가 scaffold 대신 이 흐름을 실행한다. → ticket 05

## Open questions

- Q1 (PENDING_DECISION) 쓰기 중단 장치: PoC는 PgBouncer `PAUSE`/`RESUME`으로 연결을 끊지 않고 쓰기를 멈췄다. 원본 앱은 PgBouncer 없이 DB에 직접 붙는다. 후보: (a) Data Migration이 원본 쪽에 PgBouncer를 두고 앱 연결을 바꾼다(최초 1회 재시작), (b) 원본 DB를 read-only로 바꾸고 연결을 끊는다(앱 오류 발생). ticket 04 착수 전에 정한다.
- Q2 복제 경로: 논리 복제는 대상 DB가 원본 DB에 직접 접속해야 한다. AWS→GCP·EKS→EKS에서 원본 DB(`CLUSTER_INTERNAL`)를 대상 클러스터에서 어떻게 접근할지(내부 LB, WireGuard 터널) 정해지지 않았다. ON_PREM은 터널(`tunnel_endpoint`)과 MNP Preflight를 쓴다.
- Q3 원본 DB 접근 참조: 원본 EKS kubeconfig·Namespace·Pod·Secret 위치를 어디서 받는지(분석 결과, 연결 관리 API #118 중 무엇).
- Q4 웹 계약 버전: 웹 저장소 main의 `contract/events.schema.json`에는 `data.progress`·`data.verify`만 있다. 플랫폼 사본에는 `data.preflight`·`data.phase`·`data.lsn`·`data.table`·`data.readiness`가 있고, 그 뒤 웹 2cf5f5b(미반영, 브랜치 `claude/provisioning-24-events-6a89a0`의 a800978)에 `cutover.plan`·`cutover.ready`·`cutover.outcome`·`cutover.verify`가 추가됐다. 웹 화면이 `data.*`를 그리려면 웹 저장소 반영이 필요하다.
- Q5 DB 전환 승인: `approval.required gate: cutover`(두 번째 승인)를 둘지.
