# Ticket 04 — DB 전환·롤백과 정합성 확인

## 상태

DONE(로컬 k3d 검증, PR 전). 남은 일: 두 번째 승인(Q5, 전환 담당과 결정), 실제 EKS→GKE 시험(ticket 06 WireGuard 후)

## 목적

PoC `05_switchover_fast.py` 순서로 DB 쓰기 주체를 원본에서 대상으로 바꾸고, 같은 실행기를 반대 방향으로 돌려 롤백한다.

## 구현

- 전환 실행기 `infra/data-migration/cutover/switchover.py`: PoC 스크립트를 옮긴 것. cmp-cutover Pod의 runner 컨테이너에서 접속을 유지한 채 한 번에 실행한다(SQL마다 `kubectl exec`를 하면 쓰기 중단이 수백 ms가 되므로). 설정은 stdin JSON, 결과는 stdout JSON 한 줄. PAUSE 안에서 실패하면 구독 상태를 되돌리고 원래 DB로 재개한다.
- `cmp_data.cutover.runner_config`: 방향별 설정 조립. 역방향 구독은 옛 주 DB에서 새 주 DB의 `peer_host`로 접속한다(`origin = none`).
- `dm_switchover` Activity: 옛 주 쪽 Secret에 역방향 복제 비밀번호를 두고 새 주에 `cmp_repl` 역할을 맞춘 뒤 실행기를 부른다. 성공하면 cmp-cutover Secret의 연결 대상도 새 주로 바꾸고(Pod 재생성 대비), 결과를 `cutover.step`·`cutover.result`로 낸다(`poc_bridge.switchover_events` 재사용). 전환하지 않았으면 실행기 코드(`NOT_READY`, `PAUSE_TIMEOUT`, `MARKER_TIMEOUT` 등)로 실패한다.
- 롤백: `direction: reverse`로 같은 Activity를 부른다.

## 시험

- 가짜 클러스터·DB: 이벤트가 웹 계약을 지킴, 거절 시 연결 대상 유지, 롤백 방향 설정 (`tests/data/test_data_cutover.py`)
- data-lab(k3d 두 개, 초당 약 50건 쓰기 중):

| 실행 | 쓰기 중단 | 검증 | 전환 중 앱 오류 |
| --- | --- | --- | --- |
| 전환 1 | 7ms | 체크섬 일치 | 0 |
| 롤백 1 | 6ms | 체크섬 일치 | 0 |
| 전환 2 | 5ms | 체크섬 일치 | 0 |
| 롤백 2 | 4ms | 체크섬 일치 | 0 |

  앱이 커밋 성공을 받은 쓰기는 모두 남았다(순번 대조, 누락 0·중복 0). 누락처럼 보인 순번 1건은 cmp-cutover Pod 재시작 중 앱이 오류를 받은 시도였다.
- 실패 경로(실습 중 실제 발생): marker 테이블 정의가 달라 PAUSE 안에서 실패 → 역방향 구독·슬롯 삭제, 동기 복제 창 해제, 원래 DB로 재개, 앱 오류 0.

## 주의

- cmp-cutover Pod를 다시 만들면 앱의 PgBouncer 연결이 끊긴다(실습에서 재시작 한 번에 앱 오류 8~9건). 전환 중에는 재시작하지 않는다.
- 전환 직전 새 주 DB의 시퀀스는 옛 주 값 + 10000으로 미리 올린다. 옛 주에만 있는 시퀀스가 있으면 실패한다(스키마 복사 뒤 DDL 금지).

## 수용 기준

- [x] 부하 중 전환에서 잃은 쓰기 0건, 체크섬 일치, 쓰기 중단 시간 기록. 롤백도 같은 기준.
- [x] 전환 거절·PAUSE 안 실패 시 원래 DB로 계속 서비스한다.
- [ ] 두 번째 승인(`approval.required gate: cutover`) 연결(Q5)
- [ ] 실제 클라우드(EKS → GKE) 시험
