# Ticket 84 — 그림자 리허설 2/3: 데이터 Workflow 관찰 단계·안전 감시·이벤트·AI 설명

## 상태
IN_REVIEW (로컬 구현·시험 완료, PR 리뷰·83 병합 뒤 데모 리허설 대기) — [Issue #363](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/363). 2026-11-08 오케스트레이터 지시로 착수. 담당 민서. **데이터 영역 코드(`cmp_data.shadow`·`DataMigrationInput.shadow`·`DataMigrationWorkflow`·`dm_announce_cutover`·`cutover_events.plan_data`) 대행 — 데이터 담당 공유 필요.** 같은 번호의 [84-data-rbac-with-namespace](84-data-rbac-with-namespace.md)와 다른 티켓이다(번호는 지시를 따름).

그림자 리허설 3개 중 2번째다: 83은 중계 큐 `shadow_*` Activity(Envoy 요청 복사·비교), 84(이 티켓)는 Workflow·이벤트·AI 설명, 85는 웹 화면.

## 배경
결승 데모는 온프레미스(Mac k3d)로 실제 이전을 한다. 순서는 데이터 복제(CDC) → DB 전환(2차 승인, PgBouncer) → 트래픽(중계 Envoy 가중치) → 완료다. 복제가 따라잡은 뒤 DB 전환 승인 전에, 중계 Envoy로 들어온 실제 GET 요청을 대상에도 복사해 원본 2번·대상 1번으로 3-way 비교해(83) 승인하는 사람에게 근거를 보여 준다.

**절대 원칙**: 자동 차단·자동 수정·DB 설정 변경·쓰기 복사·운영 Pod 재시작을 하지 않는다. 기본은 꺼짐이고, 끄면 지금과 같다.

## 작업 범위
- 포함: `cmp_data.shadow`(설정·판정·이벤트 값), `DataMigrationInput.shadow`, 부모가 `traffic.relay_queue`를 자식에 넘김, `DataMigrationWorkflow._shadow`(patched), `shadow.progress`·`shadow.result` 이벤트 계약·웹 스키마 사본, `cutover.plan.preconditions` `shadow` 줄, `shadow_explain` Activity(Provisioning 큐), events-guide
- 제외: `shadow_*` Activity 구현(83), 웹 화면(85), 중계 서버·데모 서버·`.env` 변경, 실제 리허설

## 공유 계약 (오케스트레이터 지정, 변경 없음)
- 83 Activity(중계 큐, 이름으로만 호출): `shadow_start({migration_id, run_id, allow_paths, percent, rate_per_sec}) -> {ok, code}`, `shadow_summary({run_id}) -> {run_id, requests, compared, match, mismatch, skipped, target_5xx, origin_p99_ms|null, target_p99_ms|null, noise_fields, samples[{method, path, status_origin, status_target, diff[{pointer, origin, target}]}]}`, `shadow_stop({run_id})` 멱등. 실패 코드 `SHADOW_UNAVAILABLE`.
- 84 이벤트(phase `data_copy`): `shadow.progress {run_id, compared, match, mismatch, elapsed_s, window_s}`, `shadow.result {run_id, state, compared, match, mismatch, match_ratio, target_5xx, p99_ratio, reason, samples(≤5), ai}`, reason `SHADOW_NO_TRAFFIC`·`SHADOW_UNAVAILABLE`·`SHADOW_STOPPED_REPLICATION`·`SHADOW_DISABLED`.
- `cutover.plan.preconditions`에 `{"id":"shadow","state":"pass"|"warn"|"skip","value":"1238/1240","evidence":"..."}` 한 줄(미실행이면 없음).
- 입력 `data_migration.shadow` 기본값 `{"enabled":false,"window_s":180,"min_requests":50,"percent":100,"rate_per_sec":20,"pass_ratio":0.99,"allow_paths":[]}`.

## 착수 결정

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | 위치 | `_replicate`에서 streaming 도달 뒤, PgBouncer 설치 완료 대기·`dm_route_app`·첫 `_announce` 전 | 승인 화면 첫 계획에 shadow 줄이 실린다. `data-early-cutover-install-v1` 실행은 설치가 이미 복사와 함께 돌고 있다(설치를 앞당긴 기존 결정을 바꾸지 않음) |
| D2 | 패치 | `enabled`일 때만 `workflow.patched("shadow-rehearsal-v1")` | 꺼진 실행은 마커도 Activity도 없어 이력이 지금과 같다. 패치 전 이력은 마커가 없어 관찰을 건너뛴다 |
| D3 | 무한 대기 방지 | `shadow_*`는 `start_to_close` 15초 × 최대 2회 + `schedule_to_close`(35초), 이벤트 발행 30초 1회, AI `schedule_to_close` 20초 1회, 감시 `dm_sample` 1회 | 중계 워커가 없어도 `schedule_to_close`로 끝난다(시험 `test_missing_relay_queue_never_hangs`) |
| D4 | 감시 | 감시 주기(`monitor_interval_s`)마다 `dm_sample` → streaming이 아니거나 실패면 즉시 `stopped`(`SHADOW_STOPPED_REPLICATION`), 그다음 `shadow_summary` → `shadow.progress` | 표본은 `last_sample`에 남아 이후 계획 알림이 같은 값을 쓴다 |
| D5 | 판정 | 계약대로. 시작 거절(`ok=false`)·수집 실패·잘못된 큐 이름도 `skipped`(`SHADOW_UNAVAILABLE`) | — |
| D6 | 정리 | `shadow_stop`은 `finally`에서 항상(시작 실패여도, 멱등) | 중계 Envoy 복사 설정이 남지 않게 |
| D7 | 막지 않음 | `_shadow`는 `Exception`을 삼키고 결과 이벤트로만 남긴다. 계획의 shadow 줄은 `plan_ready`(전환 준비 판정)에 넣지 않는다. 전환 계약 `state`에 `warn`·`skip` 추가 | 경고 전용 |
| D8 | 신호 | 관찰 중 `cancel`·`finish`·`switchover` 신호가 오면 그 주기 뒤 관찰을 멈추고 그때까지의 요약으로 판정(신호는 기존 감시 루프가 처리) | 취소를 창(최대 1시간) 동안 기다리게 하지 않는다 |
| D9 | `SHADOW_DISABLED` | 계약에 두지만 Workflow는 꺼져 있으면 아무 이벤트도 내지 않는다 | "끄면 지금과 동일"이 우선. 웹이 표시용으로 쓴다 |
| D10 | AI 경로 | 워커의 기존 `build_llm_factory`(ADR-004: 켜짐·원장·키가 있어야 함, `GovernedLLMClient` 비용 한도) — 새 Activity `shadow_explain`을 Provisioning 큐에 둔다 | `apps/api` agent_chat은 HTTP 경로라 Workflow에서 부를 수 없다. 모델이 꺼져 있으면 None → `ai=null` |
| D11 | 마스킹 | 비밀 같은 키(`password`·`secret`·`token`·`credential`·`api_key`·`private_key`) 포인터는 값을 통째로, 나머지 값·경로는 `cmp_scanners.masking.mask_text` | 이벤트 저장 때도 emitter가 한 번 더 가린다 |
| D12 | `run_id` | `shadow-<workflow.uuid4 12자>` | 결정적(재생 안전) |
| D13 | 부모 | `shadow.enabled`일 때만 자식 입력에 `shadow`·`relay_queue`(=`traffic.relay_queue`)를 넣는다 | 꺼지면 자식 입력이 지금과 같다(시험) |

## 테스트 Seam
- `tests/data/test_data_shadow.py`: 순수 판정·계약 값, Temporal 시험 환경에서 pass/warn(AI)/AI 실패/skipped(트래픽 부족·수집 실패·시작 거절)/복제 중단/중계 워커 없음, `shadow_stop` 항상, 꺼짐이면 Activity 순서 동일, 이력 재생(새 이력, 패치 전 이력).
- `tests/data/test_shadow_explain.py`: 가짜 모델로 마스킹·파싱·실패 시 None.
- `tests/data/test_data_parent.py`: 부모가 켜졌을 때만 `shadow`·`relay_queue`를 넘긴다.
- `tests/data/test_data_cutover.py`: 계획에 shadow 줄이 보이고 warn이어도 `ready`.

## 수용 기준
- [x] 꺼짐(없음·`enabled=false`)이면 Activity 순서·이벤트·자식 입력이 지금과 같다
- [x] streaming 뒤 관찰 → 판정 → `shadow_stop`(항상) → 앱 연결·계획 알림
- [x] pass·warn·skipped·stopped 판정과 이유 코드
- [x] 복제가 streaming을 벗어나면 즉시 멈춤
- [x] 어떤 결과·오류도 다음 단계를 막지 않음, 무한 대기 없음
- [x] `shadow.progress`·`shadow.result` 계약·웹 스키마 사본·events-guide
- [x] `cutover.plan.preconditions` shadow 줄(준비 판정 제외)
- [x] 불일치가 있을 때만 AI 설명, 실패·20초 초과면 null
- [x] patched 재생 호환
- [ ] 83 병합 뒤 데모 서버 worker 재배포·리허설에서 실제 Envoy 복사 확인(별도 승인)
- [ ] 웹 화면(85)

## 검증
`make check` 통과(아래 PR에 결과). OpenAPI·alembic 영향 없음(HTTP 경로·DB 변경 없음; 입력은 기존 `WEB_START_DATA_MIGRATION` JSON의 선택 필드). 데이터 담당에게 `cmp_data` 변경 공유 필요. 웹(85)은 `docs/contracts/events.schema.json`의 `shadow.*`·`warn`/`skip`을 웹 계약에 반영해야 한다.
