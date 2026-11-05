# Ticket 04 — DB 전환·롤백과 정합성 확인

## 상태

DRAFT — 사용자 검토 전. 쓰기 중단은 PgBouncer로 결정(DM-010). 전환·인계는 상헌님과 같이 맡는 영역이고 두 번째 승인(Q5)을 정해야 한다

## 목적

PoC `05_switchover_fast.py`의 순서(역방향 준비·시퀀스 사전 증가 → readiness 확인 → 쓰기 중단 → marker 도착 → 스냅샷·시퀀스 확인 → 구독 전환 → 연결 대상 변경·재개 → 정방향 정리 → 검증)를 Activity로 옮기고, `06_rollback.sh`로 되돌린다.

## 포함 범위

- 전환·롤백 Activity, 결과의 `cutover.step`·`cutover.result`(또는 웹 2cf5f5b의 `cutover.outcome`) 이벤트
- 테이블별 행 수·체크섬 비교 → `data.verify`/`cutover.verify`
- 전환 결과(쓰기 주체, 역방향 복제 상태)를 Traffic DB 게이트가 읽을 수 있게 기록

## 수용 기준(초안)

- 부하 중 전환에서 잃은 쓰기 0건, 체크섬 일치, 쓰기 중단 시간 기록. 롤백도 같은 기준.
