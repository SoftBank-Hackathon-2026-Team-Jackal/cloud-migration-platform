# Ticket 80 — 온프레미스 데이터 인계가 터널 DB 확인을 기다리게

## 상태
IN_REVIEW (로컬 구현·시험 완료, PR 리뷰·데모 서버 리허설 대기) — [Issue #319](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/319). 2026-11-07 조율 세션 지시로 착수. 담당 민서.

## 배경 (리허설 증거, 2026-11-07)
- 실행 `migration-6fccd9e3-9eed-4e73-80a6-55d098a62286`: 구축(rollout)은 통과, 부모 `MigrationWorkflow`가 `data_blocked`, `data_migration.failure_code = TUNNEL_DB_NOT_VERIFIED`로 끝났다. 데이터 자식 Workflow는 시작하지 않았다.
- 구축 결과 기록 직후 `get_migration_handoff`(DATA_MIGRATION)를 **한 번만** 읽었고, Agent의 다음 터널 보고(약 15초 주기)보다 빨랐다. 맥 Agent 로그상 그 직후 보고는 `app_health_ok:true, db_connect_ok:true`.
- 트래픽 쪽 같은 경합은 [67](67-handover-tunnel-wait.md)에서 Activity 안 대기로 풀었고, 67 D7에서 데이터 인계는 "같은 지연이 보이면 별도 티켓"으로 미뤘다. 이번 리허설이 그 증거다.

## 관련 요구사항
티켓 16 T2/T7(ON_PREM 데이터 인계), 25 F3~F5(보고 만료), 67 D1~D9, 76(온프레미스 데이터 연결). ADR-007 2안 7단계.

## 작업 범위
- 포함: `get_migration_handoff` DATA_MIGRATION 대기, 순수 판정 `tunnel_db_wait_needed`·`data_after_rollout`, `provision.check{name:"tunnel_db"}`, `MigrationWorkflow._data_migration`의 Activity timeout(patch)
- 제외: Agent 보고 주기, `cmp_data.handoff` 판정, 웹 문구(아래 D6), TRAFFIC_MIGRATION 조회(67 그대로)

## 착수 결정 (2026-11-07, Issue #319)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | 대기 위치 | 67 D1과 같이 Activity 안. Workflow는 한 번 조회하고 결과를 그대로 쓴다 | Workflow 결정성 유지, `_data_migration` 흐름 그대로 |
| D2 | 대기 대상 | consumer DATA_MIGRATION, provider ON_PREM, `blocked_by`가 모두 `TUNNEL_DB_NOT_VERIFIED`·`TUNNEL_STATUS_STALE` 중 하나일 때만(`tunnel_db_wait_needed`). 다른 코드가 섞이거나 결과 없음·AWS/GCP·TRAFFIC_MIGRATION 조회는 바로 돌려준다 | 시간이 지나면 풀리는 이유만 기다린다 |
| D3 | 구축 이후 보고 | 67 D3과 같다: 터널 `reported_at` ≥ 저장 결과 `finished_at`이어야 진행. 이전 보고로 통과한 뷰는 `TUNNEL_STATUS_STALE`, `tunnel_endpoint: null`로 막는다(`data_after_rollout`, 순수) | 이전 보고는 이전 DB를 확인한 것. 새 코드 없음. 대기 경로에서만 적용(API `handoff()`는 그대로) |
| D4 | 간격·상한 | 트래픽 설정 재사용: `TRAFFIC_HANDOVER_POLL_SECONDS`(2초)·`TRAFFIC_HANDOVER_WAIT_SECONDS`(180초, 540초로 자름). 데이터용 설정은 추가하지 않는다 | 같은 Agent 보고 주기를 기다리는 같은 문제. 설정이 하나면 데모 서버 운영이 단순하다. 따로 조정할 필요가 생기면 그때 나눈다 |
| D5 | 상한 초과 | 마지막 뷰를 그대로 돌려준다 → 기존 코드(`TUNNEL_DB_NOT_VERIFIED`/`TUNNEL_STATUS_STALE`)로 `data_blocked` | 원인이 그대로 보인다 |
| D6 | 진행 이벤트 | 기다리기 시작 `provision.check{name:"tunnel_db", state:"running"}`, 끝 `done`/`failed`. 봉투 `phase=data_copy`, status는 failed만 failed. 멱등 키 `chk:tunnel_db:<plan>:<rev>:<state>`(재시도해도 한 번). 바로 진행·바로 막힘이면 내지 않는다. **웹 문구 필요**: cloud-migration-web origin/main i18n에 `prov.check.tunnel_db.*`가 없다(`tunnel_app_health`만 있음) | 기존 계약 타입(`name` 자유 문자열), 계약 변경 없음 |
| D7 | Workflow timeout | 새 patch `data-handoff-wait-v1` 아래 `start_to_close` 600초·`heartbeat` 30초(67 상수 재사용). 대기 중 간격마다 heartbeat. patch 이전 이력은 1분으로 재생 | 상한 180초 > 기존 1분. 명령 순서는 같다 |

## 공개 인터페이스

```python
# cmp_contracts.handoff
DATA_HANDOFF_WAIT_PATCH = "data-handoff-wait-v1"
# cmp_providers.provisioning_result (순수)
TUNNEL_DB_WAIT_BLOCKS; tunnel_db_wait_needed(view: dict) -> bool
data_after_rollout(view: dict, tunnel: dict | None, recorded_at: int) -> dict
# cmp_worker.activities.approval: TUNNEL_DB_CHECK = "tunnel_db"
```

## 테스트 Seam
- Activity: `tests/test_data_handoff_tunnel_wait.py` — 67의 가짜 시계·sleep(가짜 시간이 지나면 다음 보고를 쓴다), `ActivityEnvironment`, `InMemoryEmitter`.
- 순수: `tunnel_db_wait_needed`·`data_after_rollout`.
- Workflow: `tests/data/test_data_parent.py` — time-skipping 서버에서 예약된 timeout·heartbeat 확인.
- 기존 즉시 조회 시험(`test_tunnel_exchange`, `test_provisioning_result`)은 `wait_seconds=0`으로 조회 시점의 뷰를 확인한다.

## 수용 기준
- [x] 첫 읽기 `TUNNEL_DB_NOT_VERIFIED` → 다음 보고(구축 이후, DB 확인) 뒤 2초 안 조회에서 진행, `tunnel_endpoint` 채움
- [x] 계속 막힘 → 상한 뒤 원래 코드, `tunnel_db` failed
- [x] 구축 이전 보고는 무시(그것뿐이면 `TUNNEL_STATUS_STALE`)
- [x] 다른 차단 코드·결과 없음·클라우드·TRAFFIC_MIGRATION 조회는 대기 없음
- [x] 이벤트 멱등(재시도해도 running/done 한 번씩), heartbeat
- [x] Workflow가 patch 아래 600초/30초로 예약
- [ ] 데모 서버 리허설에서 데이터 단계 진입 확인(병합·배포 뒤)

## 검증
- `make check` 통과. HTTP 경로 변경 없음 → `make openapi` 불필요. 계약 이벤트 타입 변경 없음.
