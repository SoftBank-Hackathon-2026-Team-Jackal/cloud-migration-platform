# Ticket 02 — kubectl exec psql 실행기와 사전 점검·스키마·구독 Activity

## 상태

DONE(로컬 k3d 검증, PR 전)

## 목적

Worker가 원본·대상 DB Pod 안에서 SQL을 실행해 사전 점검, 준비, 스키마 복사, publication·subscription 생성을 한다(DM-001).

## 구현

- `cmp_data.sql`: 조회 SQL(결과는 JSON 한 줄)과 해석, 생성·삭제 SQL, 인용(`literal`, `conninfo`)
- `cmp_worker.activities.data_migration`: `DbPod` 포트와 `KubectlDbPod`(kubectl exec + psql, stdin), 접근 방식별 연결(`KUBECONFIG`·`AWS_EKS`·`GCP_GKE`)
- Activity: `dm_precheck`, `dm_prepare`(REPLICA IDENTITY FULL, marker 테이블, 대상 wal_level=logical + 재시작 1회), `dm_copy_schema`(`pg_dump --schema-only`, 일부만 있으면 멈춤), `dm_subscribe`(전용 역할·Secret·발행·구독, 이름 고정이라 재실행 안전), `dm_cleanup`
- 방향(`forward`/`reverse`)에 따라 발행자·구독자를 바꾼다

## 시험 경계

- 가짜 `DbPod`: Activity 결과, 멱등성, 비밀번호가 결과에 없음, 방향 전환, 막힘 이벤트 (`tests/data/test_data_activities.py`)
- 실제 DB: k3d 클러스터 두 개(PostgreSQL 16)에서 처음부터 정리까지 (`tests/data/test_data_lab.py`, `infra/local/data-lab`). 실제 EKS·GKE 접속과 WireGuard 경로는 잡지 못한다(ticket 06).

## 수용 기준

- [x] 사전 점검이 막는 조건에서 `blocked` 단계를 내고 아무것도 만들지 않는다.
- [x] 재실행해도 스키마·역할·발행·구독이 두 번 만들어지지 않는다.
- [x] 실제 PostgreSQL 16 두 대에서 초기 복사 후 INSERT·UPDATE·DELETE(PK 없는 테이블 포함)가 반영되고 체크섬이 일치한다.
- [x] 비밀번호가 Activity 입력·결과·명령줄에 없다.
- [ ] 실제 EKS(chat-source) → 대상 클러스터 시험(ticket 06 경로 준비 후)
