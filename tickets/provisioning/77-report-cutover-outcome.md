# Ticket 77 — report 전환 결과·데모 웹 SSE 기능 빌드 인자·터널 단계 이벤트 (ADR-007 2안 8단계, 플랫폼 쪽)

## 상태
IN_REVIEW (로컬 구현·시험 완료, PR 리뷰 대기. 데모 서버 반영·실제 화면 확인 남음) — [Issue #311](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/311). 2026-11-07 조율 세션 지시로 착수.

## 배경
결선 데모는 웹 실제(SSE) 모드에서 데이터 이전 → DB 전환 → 완료 리포트까지 멈추지 않고 보여야 한다. 그런데 ① 데모 웹 빌드에 `NEXT_PUBLIC_SSE_CAPABILITIES`가 없어 리포트·트래픽 되돌리기 화면이 꺼져 있었고, ② `GET /migrations/{id}/report`에 DB 전환 결과가 없었으며, ③ 원본 EKS NLB를 준비하는 2분 30초 넘게 웹이 받는 이벤트가 없었다. 웹 저장소 쪽(Dockerfile ARG·화면)은 cloud-migration-web#34가 맡는다.

## 작업 범위
- 포함: report 최상위 `cutover_outcome`, 데모 compose web `build.args`의 `NEXT_PUBLIC_SSE_CAPABILITIES`, `dm_tunnel` 시작 때 `data.phase {phase:"tunnel"}`(계약·스키마 사본 포함), 플랫폼↔웹 이벤트 대조표
- 제외: 웹 저장소, 복제 로직, `cutover.handover` 발행 결정, 서버 반영

## 착수 결정 (2026-11-07, Issue #311)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| R1 | 필드 위치 | 응답 **최상위** `cutover_outcome`(마지막 `cutover.outcome` data, 없으면 null). 기존 `results.cutover`(cutover.result 목록)는 그대로 | 웹 세션과 합의한 이름. 거부·롤백 뒤 다시 전환하면 마지막 것만 의미가 있다 |
| R2 | 담는 키 | 웹 `CutoverOutcomeData` 중 `result, reason, active, pause_ms, steps[{id, ms, start_ms, window}], held_requests, failed_requests, replication{direction, lag_ms}`. 없는 선택 키는 빠진다 | 티켓 62 R10과 같은 화이트리스트(이중 방어) |
| R3 | `message` | 넣지 않는다 | 실행기 `detail`을 담는 자유 문자열. 웹은 `reason` 코드로 문구를 고른다 |
| R4 | SSE 기능 기본값 | `${WEB_SSE_CAPABILITIES:-report,traffic.rollback,traffic.retry}` | main에 라우트가 있는 것만(report #240, traffic rollback·retry #245). `sandbox.retry`는 플랫폼 API 없음 |
| R5 | `WEB_SSE_CAPABILITIES` 문서 위치 | `infra/demo/README.md`만. `runtime.env.example`에는 넣지 않는다 | 그 파일은 api·worker 환경 변수만 담는다는 시험(`test_runtime_example_values_point_at_the_mounted_worker_paths`)이 있다. `WEB_PLATFORM_CONNECTIONS`와 같다 |
| R6 | 터널 단계 이벤트 | `dm_tunnel` Activity가 시작할 때 `data.phase {phase:"tunnel"}`(phase `data_copy`) | `dm_precheck`가 시작 때 "preflight"를 내는 방식과 같다. Workflow 코드는 그대로라 `workflow.patched`가 필요 없다(재시도 때 같은 이벤트는 저장소가 거른다) |

## 플랫폼 ↔ 웹 이벤트 대조 (2026-11-07, origin/main ed92f7a 기준)

비교 방법: 플랫폼 계약(`event_json_schema()`)을 웹 origin/main `contract/events.schema.json`과 `tests/test_event_contract.py`의 정규형으로 비교하고, 발행 위치는 코드로 확인했다.

| 이벤트 | 플랫폼 발행 위치 | 키 비교 | 조치 |
| --- | --- | --- | --- |
| `data.phase` | `activities/data_migration.py`(preflight·blocked·복제 단계), `data_tunnel.py`(tunnel) | 플랫폼에 `tunnel` 추가 | 이 PR(계약·스키마 사본). **웹**: `DataPhase`·스키마에 `tunnel` 추가(#34 후속) |
| `data.preflight` | `activities/network_preflight.py` → `preflight_events.py`(#310부터 Workflow가 호출) | 같음 | 없음. 웹 후보 "미발행"은 #310 이전 기준이었다 |
| `data.progress`·`data.verify`·`data.lsn`·`data.table`·`data.readiness` | `data_migration.py`, `poc_bridge.py`, `cutover_events.py` | 같음 | 없음 |
| `traffic.ready`·`traffic.snapshot`·`traffic.prepare` | `workflows/traffic.py` | 같음 | 없음 |
| `cutover.plan`·`cutover.ready`·`cutover.step`·`cutover.outcome`·`cutover.verify` | `data_cutover.py`, `cmp_data/cutover_events.py` | 같음 | 없음 |
| `cutover.result` | `poc_bridge.py`(PoC 다리만) | 같음 | 실제 실행은 내지 않는다. 웹은 `cutover.outcome`·report `cutover_outcome`을 쓴다 |
| `cutover.handover` | 없음(`event_stream.py`가 읽기만) | 같음 | 보고만: 인계 항목(dns·origin·access) 상태는 Traffic·운영 결정이다(`cutover.plan.handover`가 `[]`). 웹 #34는 빈 계획이면 완료로 본다 |
| 오류 코드 `NETWORK_PREFLIGHT_FAIL/UNKNOWN`·`PREFLIGHT_INPUT_CHANGED` | `workflows/data_migration.py` → 부모 신호 `failure_code` → `error` 이벤트 `code` | — | 없음. 코드가 있고 버려지지 않는다(`test_data_workflow_onprem.py`). **웹**: 문구 추가 |
| `cutover.plan.steps[].ms` | `cutover_events.plan_data` | — | 보고만: 첫 전환 전에는 측정값이 없어 0, 전환 뒤에는 지난 실행 값. 단계별 리허설 값을 넣으려면 데이터 쪽 결정 |
| (범위 밖) `build.fix.new_env_keys` | — | 웹에만 있음 | 기록만 |
| (범위 밖) `plan.ready.scope` | — | 플랫폼에만 있음 | 기록만 |
| 사전 점검 경로 코드 | — | **웹**: `ROUTE_NOT_TUNNEL`이 아니라 `NO_ROUTE`·`UNEXPECTED_PATH`·`ROUTE_UNVERIFIED` | 웹 |

## 수용 기준
- [x] report `cutover_outcome`: 없음 → null, 있음 → 웹 모양, 여러 번 → 마지막, `message` 비노출, 실제 `run_events` 결과 통과
- [x] 데모 compose web 빌드 인자 고정 시험, README
- [x] `dm_tunnel` 시작 때 `data.phase` "tunnel", 계약·스키마 사본
- [x] `make openapi`, `make check`
- [ ] 데모 서버 반영 뒤 실제 화면 확인(웹 #34와 함께)

## 시험
`tests/test_migration_report.py`, `tests/test_demo_runtime.py`, `tests/data/test_data_tunnel.py`, `tests/test_event_contract.py`. `make check`.

## 문서
`docs/contracts/openapi.yaml`, `docs/contracts/events.schema.json`, `infra/demo/README.md`, ADR-007 단계 표, `specs/provisioning/tasks.md`.
