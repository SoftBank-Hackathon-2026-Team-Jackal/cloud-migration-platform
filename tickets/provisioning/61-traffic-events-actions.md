# Ticket 61 — 실제 모드 트래픽 이벤트(traffic.prepare·ready)와 rollback·retry API

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰 대기) — [Issue #234](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/234). 2026-11-06 조율 세션 지시로 착수. 담당 민서.

**트래픽 범위 대행**: 트래픽 전환은 이상헌님 담당 범위다. 본선(11/7~8) 일정 때문에 Provisioning이 대행한다. 전환 판단(`cmp_traffic`의 순수 함수·게이트 기준·롤백 기준)은 바꾸지 않았다.

## 배경
웹 트래픽 전환 화면은 `traffic.prepare`·`traffic.ready`·`traffic.snapshot`으로 그려진다. `traffic.snapshot`만 발행됐다(#129). 웹 실제 모드(`sseProvider.ts`)는 `POST /migrations/{id}/traffic/rollback`·`/traffic/retry`를 부르는데 API가 없었다. worker에는 `traffic_rollback` Activity가 있다.

## 관련 요구사항
명세 [migration-events.md](../../specs/provisioning/migration-events.md), 안내서 [events-guide.md](../../docs/engineering/events-guide.md), 트래픽 [ADR-006](../../docs/decisions/ADR-006.md), 웹 `src/lib/migration/{script.ts,mockProvider.ts,trafficSim.ts,sseProvider.ts,reducer.ts}`.

## 선행 작업
#63(TrafficShiftWorkflow), #128(부모가 자식으로 실행), #129(`traffic.snapshot` 발행).

## 작업 범위
- 포함
  - `TrafficShiftWorkflow`: `traffic.ready`(running → done/waiting), `traffic.prepare`(running → done/failed) 발행, `request_rollback`·`request_retry` Signal, `status` Query의 `actions`·`attempt`
  - `traffic_prepare` Activity: 대상 헬스 확인(`health_path`) 추가, 확인 결과 `checks` 반환
  - `traffic_readiness` Activity(Workflow 큐): 같은 실행의 `data-<migration_id>` `status` Query로 지연·트랜잭션·커밋 값 읽기
  - `dm_sample` 반환값에 `lag_ms` 한 줄 추가(데이터 이전 동작은 그대로)
  - `routes/traffic_actions.py`: `POST /migrations/{id}/traffic/rollback`, `POST /migrations/{id}/traffic/retry`
- 제외
  - 부모 `workflows/migration.py` 변경(없음). 계획·승인, 분석·샌드박스, `routes/migrations.py`, `infra/demo/*`
  - 게이트 실패 롤백 기준 변경(#61 "직전 안전 값" vs #63 "원본 100%" 결정은 트래픽 담당 몫으로 남김)

## 착수 결정 (2026-11-06, Issue #234 표 + 구현 중 결정)

| ID | 항목 | 결정 | 근거 |
| --- | --- | --- | --- |
| T1 | 담당 | 트래픽 범위지만 본선 일정 때문에 Provisioning이 대행한다 | Issue #234 |
| T2 | rollback·retry | 구현한다. API는 Signal만, 되돌림은 기존 `traffic_rollback` Activity. 자동 재시도·자동 복귀 없음(사람이 버튼으로 요청) | 무중단 이전·롤백은 심사 기준. 기존 원칙 유지 |
| T3 | 라우트 위치 | 새 파일 `routes/traffic_actions.py` | 다른 세션과 충돌 줄이기 |
| T4 | Signal 대상 | 자식 `traffic-<migration_id>`(부모 `_shift_traffic`의 ID 규칙). 부모는 바꾸지 않는다 | 부모는 60 세션이 수정 중. 자식이 끝나야 부모가 다음으로 간다 |
| T5 | 롤백 뒤 대기 | 웹 실제 모드(`migration_id` 있음)에서는 롤백 뒤 원본 100%로 `retry_wait_seconds`(기본 900초) 동안 재시도를 기다린다. 기한이 지나거나 취소되면 기존처럼 `ROLLED_BACK`으로 끝난다. `migration_id`가 없으면 기존과 같이 바로 끝난다 | 재시도 버튼이 동작하려면 자식이 살아 있어야 한다. 무한 대기는 부모를 묶는다 |
| T6 | 롤백 스냅샷 상태 | 재시도를 기다리는 동안 `rolledback` 스냅샷은 `status: running`(목업과 같음), 기한이 지나면 같은 스냅샷을 `failed`로 한 번 더 낸다 | 웹은 `failed` 이벤트를 이전 실패로 굳혀 재시도 버튼을 쓸 수 없다 |
| T7 | 수동 롤백 응답성 | 관찰 창(`traffic_observe`)을 기다리지 않고 바로 되돌린다(관찰 Activity는 취소 요청, 결과는 버린다) | 기본 관찰 창 120초 |
| T8 | 재시도 | 같은 기준선으로 첫 단계부터 다시 한다(`traffic_prepare` 재실행 없음). 첫 스냅샷에 `notes: [{key: restart}]` | 목업 `TrafficSim.retry`와 같은 순서 |
| T9 | 409 판단 | 자식 `status` Query의 `actions`(`rollback`: 적용·관찰·판정 중, `retry`: 되돌린 뒤 대기 중). 자식이 없거나 끝났으면 `TRAFFIC_NOT_RUNNING`, 지금 받을 수 없으면 `TRAFFIC_ACTION_NOT_ALLOWED`. Signal 처리도 같은 조건으로 무시(멱등) | Query와 Signal 사이 경합에도 상태가 깨지지 않는다 |
| T10 | 사전 확인 | `handoff_status`·`ready`는 Workflow가 인계값으로, `external_endpoint`(중계 대상 일치)·`health`(대상 `health_path` GET 2xx·3xx, 3회)는 `traffic_prepare`가 확인한다. 실패 이유는 코드만(`HANDOVER_NOT_READY`·`RELAY_NOT_CONFIGURED`·`TARGET_MISMATCH`·`TARGET_UNHEALTHY`·`PREPARE_FAILED`) | 건강하지 않은 대상으로 보내지 않는다. 중계 서버 워커가 Envoy와 같은 곳에서 묻는다 |
| T11 | 이벤트 발행 위치 | 모두 Workflow가 `emit_migration_event`로 낸다(`idempotency_key` `traffic.prepare:<state>`·`traffic.ready:<state>`) | 중계 서버 워커(`workers/relay.py`)에는 이벤트 저장소가 없다 |
| T12 | traffic.ready 값 | 데이터 이전(`data-<id>`)의 최근 `lag_ms`·`longest_xact_ms`·`commits_per_sec`. 하나라도 모르면 내지 않는다. 상태는 시작 때 `running`, 사전 확인 뒤 `done`(DB 게이트가 100% 단계를 막으면 `waiting`) | 0을 지어내지 않는다. 웹은 없으면 `data` 값으로 대신 그린다 |
| T13 | 대상 이름 | `traffic.prepare.target.name`은 `external_endpoint`의 호스트. 원본 이름은 보내지 않는다(웹 기본 문구) | 플랫폼이 아는 값만 |

## 이벤트 순서 (웹 `mockProvider.ts` traffic 단계와 같은 순서)
1. `traffic.ready` running(값을 알 때)
2. `traffic.prepare` running — `handoff_status`·`ready` done, `external_endpoint` running
3. `traffic_prepare` Activity → `traffic.prepare` done(네 확인 모두 done) 또는 failed(`reason`)
4. `traffic.ready` done/waiting
5. `traffic.snapshot` applying → observing → gate → … → complete, 또는 rolledback

## 수용 기준
- [x] 이벤트 계약 검증(`validate_data`), 중복 없음(키 4개 고유), 비밀값 비노출 — `tests/traffic/test_traffic_actions.py`
- [x] Signal: 수동 롤백 → 재시도 → 완료, 잘못된 상태의 Signal 무시, 대기 중 취소, 재시도 없을 때 실패 보고, Replay — 같은 파일
- [x] API: 없는 migration 404, 상태가 맞지 않으면 409(코드만), Temporal 불가 503, 실제 Temporal로 rollback·retry — `tests/traffic/test_traffic_actions_api.py`
- [x] `make check`, `make openapi`
- [ ] alembic check: 모델 변경 없음. 로컬 DB(5433)가 없어 CI에서 확인
- [ ] 리뷰, 데모 서버에서 실제 중계 서버로 확인

## 문서
- [events-guide.md](../../docs/engineering/events-guide.md) 트래픽 절에 실제 발행 단락 추가
- [openapi.yaml](../../docs/contracts/openapi.yaml) 재생성
