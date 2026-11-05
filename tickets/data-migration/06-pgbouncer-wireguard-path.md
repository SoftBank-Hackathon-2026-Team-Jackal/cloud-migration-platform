# Ticket 06 — 원본 PgBouncer 배치와 WireGuard 복제 경로

## 상태

DRAFT — 사용자 검토 전. 결정: 쓰기 중단은 PgBouncer(DM-010), 복제 경로는 WireGuard(DM-011)

## 목적

PoC의 전제(앱이 PgBouncer를 거쳐 DB에 붙음, 두 DB가 사설 경로로 서로 닿음)를 실제 환경에 만든다.

## 포함 범위

- 원본 클러스터에 PgBouncer 배포(`pool_mode=transaction`, 관리 사용자, `databases.ini` 교체 가능), 원본 앱 DB 연결을 PgBouncer로 변경(1회 재시작). 연결 문자열은 Secret에만 둔다(database_url 규칙)
- WireGuard 터널: 대상 DB → 원본 DB(정방향 구독), 원본 DB → 대상 DB(역방향 구독, 전환 뒤 PgBouncer). MTU 1420(PoC·ADR-006 실측)
- ON_PREM은 기존 Agent 터널(#71)을 쓰고 새로 만들지 않는다

## 미정

- AWS·GCP 대상에서 터널 양 끝 위치(노드, Pod 사이드카, 별도 VM)와 터널 생명주기 소유(Provisioning 또는 Data)
- PgBouncer 배치 시점(복제 시작 전 또는 전환 직전)과 앱 재시작 승인 여부

## 수용 기준(초안)

- 원본 앱이 PgBouncer를 거쳐 오류 없이 동작하고, `PAUSE`/`RESUME`이 앱 오류 없이 동작한다.
- 대상 DB Pod에서 원본 DB로, 원본 DB Pod에서 대상 DB로 터널을 통해 `psql` 접속이 된다.
