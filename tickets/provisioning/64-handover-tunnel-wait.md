# Ticket 64 — ON_PREM 트래픽 인계 조회가 터널 앱 헬스 확인을 조건으로 대기

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰·데모 서버 확인 대기) — [Issue #284](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/284). 2026-11-07 조율 세션 지시로 착수. 담당 민서.

> 번호 주의: "Provisioning 64"라는 제목은 #280·#281(PR #282, 헬스 경로·ON_PREM 구축 이벤트)에서도 쓰였다. 이 문서는 `64-handover-tunnel-wait.md`로 구분한다. 번호 정리는 조율 세션 결정을 따른다.

## 배경
2026-11-07 데모 서버 ON_PREM 실행(`migration-a224dfbd…`, 릴리스 98b359d)에서 구축은 성공(`SUCCEEDED READY`)했다. 그런데 `get_traffic_handover`가 19:37:35Z에 **한 번만** 조회돼 `external_endpoint: null`, `blocked_by: [EXTERNAL_ENDPOINT_UNAVAILABLE]`로 `traffic_blocked` 종료됐다.

ON_PREM 외부 주소(`http://<tunnel_ip>:<app port>`)는 터널 상태 `app_health_ok=true`·stale 아님일 때만 채워진다(티켓 16 T11, 25 F3). 앱 포트는 롤아웃 결과 기록 뒤 터널에 배정되고, 터널 컨테이너는 다음 보고에서 헬스를 확인한다. 실제 기록: 터널 로그 `APP_PORT_NOT_ASSIGNED` → 19:38:41Z 보고에서 `app_health_ok: true`. 조회보다 약 1분 늦었다.

## 관련 요구사항
티켓 16 T11(ON_PREM 트래픽 목적지), 25 F3~F5(터널 보고 만료), 50 H4/H6(인계 조회), #128(전환 입력). ADR-006.

## 작업 범위
- 포함
  - `get_traffic_handover` Activity 안의 대기(짧은 간격 재조회, 상한, heartbeat, `provision.check` 진행 이벤트)
  - 롤아웃 이후 보고만 근거로 인정(`after_rollout`), 터널 뷰에 `reported_at`
  - `MigrationWorkflow`: 새 patch 아래 이 Activity의 타임아웃·heartbeat 타임아웃만 변경
- 제외
  - Agent 터널 컨테이너 즉시 확인·보고(티켓 65, `agent_tunnel.py`)
  - 데이터 이전 인계(`get_migration_handoff`) 대기 — D7
  - 새 이벤트 타입·계약 변경·웹 변경

## 착수 결정 (2026-11-07, Issue #284)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | 대기 위치 | Activity 안에서 기다린다. Workflow는 한 번 조회하고 결과를 그대로 쓴다(코드 흐름 그대로) | Workflow 결정성 유지. Workflow 쪽 timer 반복은 이력이 길어지고 `_read_traffic_handover` 흐름을 바꾼다 |
| D2 | 대기 대상 | ON_PREM이고 `blocked_by`가 모두 `EXTERNAL_ENDPOINT_UNAVAILABLE`·`TUNNEL_STATUS_STALE` 중 하나일 때만(`tunnel_wait_needed`). 결과 없음·`APPLICATION_NOT_READY`·`PROVISIONING_NOT_READY` 등 다른 이유와 AWS·GCP는 지금처럼 바로 돌려준다. 기다리는 중 다른 이유가 나타나면 즉시 멈추고 그 값을 돌려준다 | 시간이 지나면 풀리는 이유만 기다린다. 다른 이유는 기다려도 풀리지 않는다 |
| D3 | 진행 조건 | 터널 보고의 `reported_at` ≥ 저장된 결과의 `finished_at`(롤아웃 결과 기록 시각)이고 기존 판정(앱 헬스 통과·stale 아님)이 통과할 때. 롤아웃 이전 보고로는 뷰가 통과해도 `TUNNEL_STATUS_STALE`로 막는다(`after_rollout`, 순수 함수). 두 시각 모두 API·Worker의 같은 서버 시계(epoch 초) | 롤아웃 전 보고는 이전 앱(또는 포트 없음)을 확인한 것이다. 기존 코드 중 "근거가 너무 오래됨"과 같은 의미라 새 코드를 만들지 않는다 |
| D4 | 간격·상한 | 2초마다 다시 읽는다(`TRAFFIC_HANDOVER_POLL_SECONDS`, 기본 2.0, 최소 0.1). 상한은 `TRAFFIC_HANDOVER_WAIT_SECONDS`(기본 180초). 상한은 Activity 타임아웃 − 60초(540초)로 자른다 | 터널 보고 주기(15초)·보고 만료(60초)보다 넉넉하게 3분. 2초 간격이면 보고 뒤 2초 안에 진행하고 DB 읽기는 가볍다(PK 조회 2건) |
| D5 | 상한 초과 | 마지막 뷰를 그대로 돌려준다 → `blocked_by`가 기존 코드(`EXTERNAL_ENDPOINT_UNAVAILABLE` 또는 `TUNNEL_STATUS_STALE`) 그대로 `traffic_blocked` | 원인이 그대로 보인다 |
| D6 | Workflow 타임아웃 | `traffic-handover-wait-v1` patch 아래 `start_to_close_timeout` 600초, `heartbeat_timeout` 30초(재시도 3회·`schedule_to_start` 5분은 그대로). 대기 중 매 간격 heartbeat. patch 이전 이력은 1분 타임아웃으로 그대로 재생된다 | 상한 180초가 기존 1분 타임아웃을 넘는다. heartbeat가 Worker 중단을 30초 안에 알린다. Activity 옵션만 바뀌어 명령 순서는 같다 |
| D7 | 데이터 인계 | 이번에는 적용하지 않는다. `get_migration_handoff`(DATA_MIGRATION, `TUNNEL_DB_NOT_VERIFIED`)는 같은 구조지만 데이터 담당의 `_data_migration` 호출(1분 타임아웃, heartbeat 없음)·`cmp_data.handoff` 판정을 함께 바꿔야 한다. 같은 헬퍼(`_read`)로 쉽게 붙일 수 있게 나눴다 | 데모 실패는 트래픽 인계였다. DB 확인이 롤아웃 직후 같은 지연을 겪는지는 실제 기록으로 확인하지 않았다. 같은 지연이 보이면 데이터 담당과 별도 티켓 |
| D8 | 진행 이벤트 | 기다리기 시작할 때 `provision.check{name:"tunnel_app_health", state:"running"}`, 끝날 때 `done`(진행) 또는 `failed`(상한·다른 이유). 봉투 `phase=traffic`, status는 failed만 failed, 나머지 running. 멱등 키 `chk:tunnel_app_health:<plan>:<rev>:<state>`. 바로 진행·바로 막힘이면 내지 않는다 | 기존 계약 타입(`ProvisionCheckData.name`은 자유 문자열). 새 타입 없음. 지금 단계가 `traffic`이라 웹 단계가 뒤로 가지 않는다 |
| D9 | 65와의 관계 | 이 티켓만으로 원큐 진행(최대 터널 보고 주기만큼 대기). 65가 포트 배정 즉시 보고하면 몇 초 안에 진행 | 두 티켓 파일이 겹치지 않는다(`agent_tunnel.py` 미수정) |

## 공개 인터페이스

```python
# cmp_contracts.handoff
TRAFFIC_HANDOVER_WAIT_PATCH = "traffic-handover-wait-v1"
TRAFFIC_HANDOVER_START_TO_CLOSE_S = 600; TRAFFIC_HANDOVER_HEARTBEAT_S = 30; TRAFFIC_HANDOVER_WAIT_MAX_S = 540
# cmp_providers.provisioning_result (순수)
TUNNEL_WAIT_BLOCKS; tunnel_wait_needed(TrafficHandoff) -> bool
after_rollout(TrafficHandoff, tunnel: dict | None, recorded_at: int) -> TrafficHandoff
# cmp_api.tunnels.tunnel_view(...) 결과에 reported_at 추가
# cmp_worker.activities.approval.ApprovalActivities(session_factory, clock=None, *, timer, sleep, wait_seconds, poll_seconds)
```

## 테스트 Seam
- Activity: SQLite 저장 결과 + `agent_tunnel_status` 행, 가짜 시계·sleep(가짜 시간이 지나면 다음 보고를 쓴다), `ActivityEnvironment`(heartbeat), `InMemoryEmitter`.
- 순수: `tunnel_wait_needed`·`after_rollout`.
- Workflow: Temporal time-skipping 서버, 실제 `MigrationWorkflow`, Provisioning 가짜 뒤의 실제 Activity. 실행 이력 Replay, 예약된 타임아웃 확인. 티켓 50 이전 이력 Replay는 기존 테스트가 계속 확인.

## 수용 기준
- [x] 처음 헬스 실패 → n초 뒤 통과 보고가 오면 다음 조회(2초 안)에 진행
- [x] 롤아웃 이전 보고는 무시(그것뿐이면 상한 뒤 `TUNNEL_STATUS_STALE`)
- [x] 상한 초과 시 기존 코드(`EXTERNAL_ENDPOINT_UNAVAILABLE`)로 종료, `provision.check failed`
- [x] AWS·GCP·결과 없음·다른 막힘은 대기 없음
- [x] Temporal 테스트 서버 통합 테스트 1개, Replay
- [x] `make check`
- [ ] 데모 서버 ON_PREM 원큐 실행 확인(배포 뒤, 조율 세션)

## 문서
`specs/provisioning/shared-contracts.md`(인계 조회), `specs/provisioning/migration-events.md`(E22), `docs/architecture/temporal-flow.md`(50 문단), `specs/provisioning/tasks.md`.
