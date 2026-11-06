# Ticket 07 — DB 전환 두 번째 승인과 웹 cutover 이벤트

## 상태

DONE(로컬 검증). Q5를 웹이 이미 정한 방식(`approval.required gate: cutover` → `POST /approve?gate=cutover`)으로 연결했다.

## 구현

- API `POST /migrations/{id}/approve?gate=cutover`: 자식 `data-<id>`의 `status`를 읽어 원본이 쓰기 주체이고 `streaming`일 때만 `switchover` 신호를 보낸다. 아니면 409 `CUTOVER_NOT_READY`(현재 `phase` 포함), 데이터 이전이 없는 실행이면 404. `gate` 없는 승인은 예전과 같다.
- `DataMigrationWorkflow`(`data-cutover-announce-v1` patch): 원본이 쓰기 주체일 때 `dm_announce_cutover`로 `cutover.plan`을 낸다. 복제 준비 상태나 시도 수가 바뀔 때만 다시 내고, 새 시도(처음, 거부 뒤, 롤백 뒤)마다 `approval.required`(gate `cutover`)도 함께 낸다. 알림 실패는 이전을 막지 않는다.
- 전환 실행기(`switchover.py`) 결과에 `steps`(단계별 ms, PAUSE 기준 시작 시각), `ready`(PAUSE 직전 준비도 표본, 기다린 시간), `tables`(테이블별 행 수·일치)를 더했다. 거부돼도 그때까지의 `steps`와 `ready`, PAUSE 여부를 낸다.
- `cmp_data.cutover_events`: 결과 → `cutover.ready` → `cutover.step`(단계마다) → `cutover.outcome` → 통과면 `cutover.step`(verify), `cutover.verify`(테이블마다), `data.verify`(rows·checksum). 거부면 실패한 단계, `resume_source`(revert), `refused` 결과만 낸다. 실행기에 닿지 못해도(kubectl 실패) `refused`를 낸다.
- 계약: 웹 계약의 `cutover.plan`·`cutover.ready`·`cutover.outcome`·`cutover.verify`와 `cutover.step`(id·ms·method·of·result·window, n ≤ 32)·`cutover.handover`(held·detail) 확장을 `cmp_contracts.events`와 `docs/contracts/events.schema.json`에 반영했다. 다른 영역의 웹 계약 변경(analysis.scan, plan.preview.backend 등)은 넣지 않았다.
- 예전 PoC 연결(`poc_bridge.switchover_events`, `cutover.result`)은 Activity에서 쓰지 않는다. `cutover.result`를 `done`으로 내면 웹이 이전 전체를 끝난 것으로 보기 때문이다(DB 전환 뒤에 트래픽 단계가 남아 있다).

## 시험

- `tests/data/test_data_cutover_events.py`: 결과 → 이벤트(롤백 방향, 불일치, 검증 생략, PAUSE 전 거부, 준비 대기 사유), 실행기 단계 기록·테이블 비교, 승인 API(신호, 409, 404, 기존 승인)
- `tests/data/test_data_cutover.py`, `tests/data/test_data_workflow.py`: 이벤트 순서·계약 검증, 승인 요청은 시도마다 한 번
- data-lab(초당 약 45건 쓰기 중) + 로컬 Temporal: 준비 전 승인은 409, streaming 뒤 계획·승인 요청 → API 승인 → 전환 5.48ms → 롤백 4.97ms → 다시 계획·승인 요청. 테이블 4개 행 수·체크섬 일치, 모든 이벤트 계약 검증 통과

## 남은 일

- 웹 화면 순서: 웹은 DB 전환 승인 화면을 트래픽 다음(cutover 단계)에 두지만, 플랫폼은 DB를 먼저 전환한 뒤 트래픽을 옮긴다(DM-007, Traffic #128과 합의). 웹에서 `approval.required gate: cutover`를 데이터 이전 화면 끝에서 받도록 바꿔야 한다.
- 계획의 `handover`는 비워 둔다(DNS·원본 보존·접근 권한 인계는 Traffic·운영 쪽).
