# Ticket 50 — Provisioning 인계 조회 방식과 Traffic Handover 정합 (#128 선행)

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 조율 세션 리뷰·이상헌님 확인 대기) — [Issue #185](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/185). 2026-11-06 조율 세션 지시로 착수(2차 병렬 묶음 44·46·52·50·54). 49 후속.

## 배경 (실동작 점검 A-04 중 김민서 부분, A-05, A-14, 흐름 점검 N5·N6)
- A-04/N5: `MigrationWorkflow`가 실행 뒤 인계(`get_migration_handoff`)를 읽지 않는다. 데이터 이전은 #138에서 `_data_migration`이 DATA_MIGRATION 뷰를 읽게 됐지만, Traffic 쪽은 여전히 scaffold `shift_traffic`에 `project_id`만 넘긴다. 이상헌님 #128(상위 Workflow의 `shift_traffic`을 `TrafficShiftWorkflow` 자식으로 교체)은 "Provisioning 인계값 조회 방식 확정"을 선행 조건으로 기다린다.
- A-05/N6: 인계 뷰(`{common:{status,...}, application:{external_endpoint, external_host, external_port, port, ...}, proceed, blocked_by}`)와 Traffic `Handover`(`{status: READY|FAILED|UNKNOWN, ready, external_endpoint, health_path, port=80}`)의 구조·status·port가 다르다. AWS 결과는 `port`(앱 8080)와 `external_port`(LB 80)를 함께 가져 잘못 고르기 쉽다. `INFRASTRUCTURE_READY`는 `Handover.status`로 표현할 수 없다.
- 이상헌님이 #128 준비로 `cmp_traffic.handover.handover_from_result`(저장된 **결과**를 읽음)를 추가했다(f2b5d15). ON_PREM의 터널 쪽 목적지는 결과가 아니라 **조회 시점의 뷰**에만 채워지므로(16 T6·T11, 25 F3) 결과로는 ON_PREM 전환을 시작할 수 없고 `blocked_by`도 전달되지 않는다.
- A-14(추정): AWS/GCP DB 인계 주소가 클러스터 내부 DNS(`<db>.<ns>.svc`)뿐이다. 데이터 담당(soulee-dev)이 #185 댓글에서 "Worker는 `kubectl exec`로 DB Pod 안에서 SQL을 실행하고 클러스터 사이 접속은 WireGuard 쪽 주소(`peer_host`)로 하므로 문제되지 않는다"고 답했다.

## 관련 요구사항
PROV-018, PROV-019, PROV-020, PROV-021. 계약: [shared-contracts §Provisioning 결과 인계](../../specs/provisioning/shared-contracts.md#provisioning-결과-인계-13), [티켓 13 R7](13-result-handoff.md), [16 T6·T11](16-tunnel-config-exchange.md), [25 F3](25-tunnel-status-freshness.md).

## 선행 작업
Blocked by: 13, 16, 25, 49 (main 반영). 후속: 이상헌님 #128.

## 작업 범위
- 포함
  - 상위 Workflow가 부를 Traffic 인계 조회 Activity(이름·입력·출력)와 막힘·조회 실패 코드를 `cmp_contracts.handoff`와 문서에 고정
  - 인계 뷰 → Traffic `Handover` 변환기(Provisioning 쪽, 순수 함수)와 실제 `cmp_traffic.contracts.Handover`로 검증하는 계약 테스트
  - `MigrationWorkflow`의 실행 성공 뒤 Traffic 인계 조회 지점(`workflow.patched`), 결과 보존, 막힘 시 전환 미시작
  - DB 인계(DATA_MIGRATION) 필드 정리와 데이터 이전 입력 계약 테스트
  - `phase` 변경 때마다 `data_migration`이 지워지던 문제 수정(#128 `db_gate_ok` 근거 보존, H8)
- 제외: `shift_traffic` → `TrafficShiftWorkflow` 자식 교체(#128), `packages/traffic` 수정(이상헌님), CDC 경로 설계(데이터 담당), HTTP 응답 모델 변경, 시작 입력(44)

## 변경 경계 (병렬 티켓과 충돌 최소화)
- `apps/worker/src/cmp_worker/workflows/migration.py`
  - import 블록: `cmp_contracts.handoff`의 3개 이름 추가
  - `__init__`: `data_migration`(phase setter에서 이동)·`traffic_handoff` 초기화 2줄. `phase` setter에서 `data_migration` 줄 삭제
  - `status` query: `traffic_handoff` 키 1줄
  - 새 메서드 `_read_traffic_handover` (`_step` 바로 앞)
  - `_run`의 **두 번째 루프**(`provisioning`·`data_copy`·`traffic`·`verification`)에서 `data_copy` 분기 다음, `await self._step(...)` 바로 앞의 `traffic` 분기. 44가 고치는 `_run` 앞부분(시작 입력·분석·Sandbox)과 `_hand_off`는 건드리지 않았다
- `packages/contracts/src/cmp_contracts/migration.py`: `FAILURE_OUTCOMES`·`OUTCOME_PHASE`에 `traffic_blocked` 1줄씩. 49 후속(D-19)이 고치는 `cmp_api.migration_state`·`event_stream`은 건드리지 않았다(새 종료 phase는 기존 규칙대로 `failed`)
- 새 파일: `cmp_contracts.handoff`. 추가: `cmp_providers.provisioning_result.traffic_handover`, `ApprovalActivities.get_traffic_handover`, `workers/provisioning.py` 등록 1줄

## 수용 기준 (Issue #185)
- [x] #128이 쓸 인계 조회 Activity와 변환 결과가 계약 테스트로 고정된다. (`tests/test_traffic_handoff.py`)
- [ ] 이상헌님 확인. (PR에서 요청, "협의 필요" 참고)

## Test Seam
- 계약: 실제 `build_provisioning_result` → 실제 `handoff` 뷰(13·16·25 규칙, ON_PREM 터널 dict 포함) → `traffic_handover` → **실제** `cmp_traffic.contracts.Handover.model_validate` + `check_handover`. Provider refs는 09·11·12에서 기록한 실제 결과 모양(티켓 13 테스트 재사용).
- Activity: SQLite + 저장된 GCP 결과 + `ActivityEnvironment`로 `get_traffic_handover`.
- Workflow: Temporal time-skipping 테스트 서버 + 실제 `MigrationWorkflow`·승인 Workflow·API 승인 전달 + 가짜 Provisioning Activity + 실제 `emit_migration_event`(`InMemoryEmitter`). 실행 순서는 이력의 ActivityTaskScheduled로 확인. 모든 실행 Replayer 재생 + 이 티켓 전 코드로 녹화한 ON_PREM 실행 이력(`tests/fixtures/temporal/migration-onprem-executed-pre50.json`) 재생.
- 데이터: 실제 `cmp_data.handoff.target_endpoint`에 실제 DATA_MIGRATION 뷰.
- 놓치는 것: 실제 중계 서버·Envoy, 실제 `TrafficShiftWorkflow` 자식(#128), 실제 클러스터·터널, 실제 PostgreSQL.

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| H1 조회 Activity | Traffic용 새 Activity `get_traffic_handover`(Provisioning 큐). 입력 `TrafficHandoverRequest{plan_id, revision}`(실행한 **워크로드** Revision, Workflow `plan`), 출력 `TrafficHandoff{contract_version:"1", plan_id, revision, provider, proceed, blocked_by, handover: TrafficHandover}`. 데이터 이전은 기존 `get_migration_handoff{plan_id, revision, consumer:"DATA_MIGRATION"}` 그대로 | 변환 결과가 Activity 결과로 이력에 남아 변환 규칙이 바뀌어도 재생이 깨지지 않는다. Workflow에서 변환하면(대안) 규칙 변경이 재생 상태를 바꾼다. #128은 `handover`를 그대로 자식 요청에 넣는다 |
| H2 변환 위치 | 변환기 `cmp_providers.provisioning_result.traffic_handover(view)`(순수). 모델 `cmp_contracts.handoff.TrafficHandover`는 Traffic `Handover`와 필드·기본값이 같고, 계약 테스트가 실제 `Handover`를 import해 필드 집합·기본값 일치와 검증 통과를 고정한다. Provisioning 런타임 코드는 `cmp_traffic`을 import하지 않는다 | 이슈 지시(Provisioning 쪽, `packages/traffic` 수정 금지). 인계 뷰의 ON_PREM 터널 병합·`blocked_by` 규칙을 아는 쪽이 변환한다. 점검 A-05의 "Traffic 쪽 `from_handoff`" 안은 Traffic이 Provisioning 판정 규칙을 다시 구현해야 한다 |
| H3 대응표 | `status`: READY→READY, FAILED→FAILED, INFRASTRUCTURE_READY·UNKNOWN·결과 없음→UNKNOWN. `external_endpoint` = `http://{external_host}:{external_port}`(둘 다 있을 때만). `port` = `external_port`(AWS/GCP LB 80, ON_PREM 터널 쪽 앱 포트), 없으면 80. 앱 `port`(클러스터 안 컨테이너 포트)는 쓰지 않는다. `health_path` = 앱 값, 없으면 `/health`. `ready` = 뷰 `proceed` ∧ status READY ∧ 목적지 있음 ∧ 앱 ready ∧ HTTP·TLS 없음(P0). `proceed` = `ready`. `blocked_by`는 뷰 값 그대로(첫 값이 원인), 뷰가 진행인데 `ready`가 아니면 `APPLICATION_NOT_READY` | A-05 port 혼동 제거. 막혀도 `handover`를 만들어 이유가 함께 가고, `proceed`를 무시한 소비자도 `check_handover`를 통과하지 못한다 |
| H4 조회 시점 | `_run` 두 번째 루프의 `traffic` 단계 직전, `provisioned == "executed"`(승인 실행 성공)이고 `workflow.patched("traffic-handoff-v1")`일 때. 데이터 단계(있으면 DB 전환까지) 뒤, `shift_traffic` 앞. scaffold 경로(`provisioned is None`)는 조회하지 않는다 | ON_PREM 목적지는 터널 상태를 읽는 시점에 정해진다(25: 60초 만료). 실행 직후 읽으면 긴 데이터 단계 뒤에 낡은 값이 된다. 데이터 단계는 이미 자기 시점에 DATA_MIGRATION 뷰를 읽는다(#138) |
| H5 결과 전달 | 조회 결과는 Workflow 상태 `traffic_handoff`와 `status` query에 보존한다. scaffold `shift_traffic`의 입력(`project_id`)은 바꾸지 않는다. HTTP 응답 모델(`MigrationStatus`)은 바꾸지 않는다(OpenAPI 변경 없음) | #128은 이 단계를 자식 Workflow로 바꾸며 `{"handover": self.traffic_handoff["handover"], "db_gate_ok": self.db_gate_ok(), ...}`를 바로 쓴다. scaffold Activity 입력을 바꾸면(대안) 곧 지울 코드에 재생 분기만 늘어난다 |
| H6 막힘·실패 | `proceed=false`면 phase·결과 `traffic_blocked`, `failure_code` = `blocked_by[0]`, `failure_issues` = `blocked_by`. 조회 Activity가 실패하면(재시도 3회, schedule-to-start 5분 포함) `failure_code=HANDOFF_UNAVAILABLE`, `traffic_handoff=null`. 어느 쪽이든 `shift_traffic`·`verify_target`을 실행하지 않는다. `traffic_blocked`는 `FAILURE_OUTCOMES`이고 실패 단계는 `traffic`이라 49의 `error` 이벤트·`MIGRATION_FAILED` 로그가 나가고 SSE는 `failed`로 닫힌다 | #128 수용 기준 "인계값 조회가 실패하면 전환을 시작하지 않는다(BLOCKED)"와 같은 의미. `data_blocked`와 같은 모양이라 화면·에이전트 처리가 같다. 터널 확인을 기다리며 재조회하는 정책은 정하지 않았다(협의 필요) |
| H7 DB 인계 | DATA_MIGRATION 뷰 필드는 바꾸지 않는다. 데이터 이전이 읽는 필드(#185 댓글): `proceed`, `blocked_by`, `common.namespace`, `common.cluster.{kind,name,location,account_id}`, `database.{release,name,tunnel_endpoint}`. 계약 테스트가 실제 `target_endpoint`로 고정한다. A-14는 데이터 담당 답변에 따라 "현재 설계에서 문제 아님"으로 닫고 새 주소 필드는 만들지 않는다 | CDC는 `kubectl exec`로 대상 DB Pod 안에서 돌고 클러스터 사이 주소는 WireGuard `peer_host`(입력)다. CDC 실행 위치가 바뀌어 대상 DB의 외부·내부 LB 주소가 필요해지면 `database`에 자리를 추가한다(협의 필요) |
| H8 data_migration 보존 | `phase` setter 안에 있던 `self.data_migration = None`을 `__init__`으로 옮긴다 | #138 병합에서 `__init__` 줄이 setter 안으로 들어가, phase가 바뀔 때마다 데이터 자식 상태가 지워졌다. 그대로면 traffic 단계에서 `db_gate_ok()`가 항상 None이라 #128의 DB 게이트가 동작하지 않는다. 쿼리 상태만 바뀌고 명령은 같아 재생에 영향 없다 |

## 구현 위치
- 계약: `cmp_contracts.handoff`(Activity 이름, patch 이름, `BLOCK_CODES`, `HANDOFF_UNAVAILABLE`, 요청·`TrafficHandover`·`TrafficHandoff` 모델), `cmp_contracts.migration`(`traffic_blocked`)
- 변환: `cmp_providers.provisioning_result.traffic_handover`
- Activity: `cmp_worker.activities.approval.ApprovalActivities.get_traffic_handover`, 등록 `cmp_worker.workers.provisioning`
- Workflow: `cmp_worker.workflows.migration.MigrationWorkflow._read_traffic_handover`와 `_run` traffic 분기

## 검증
- `tests/test_traffic_handoff.py` 17건: 모델 필드·기본값 일치, AWS·GCP READY → LB 주소·port 80(앱 8080 아님)·`check_handover` 통과, ON_PREM 터널 확인 뒤 `http://10.200.0.2:8080`, 막힘 4종(ON_PREM 미확인 `EXTERNAL_ENDPOINT_UNAVAILABLE`, 만료 `TUNNEL_STATUS_STALE`, `INFRASTRUCTURE_READY`→UNKNOWN `WORKLOADS_NOT_DEPLOYED`, 실패 `PROVISIONING_NOT_READY`) 모두 실제 `Handover`로 검증되고 precheck 거절, 결과 없음 `RESULT_NOT_FOUND`, DATA_MIGRATION 뷰 → 실제 `target_endpoint`, Activity(저장 결과·없는 Revision), Workflow 3건(성공: 조회 → `shift_traffic` 순서·상태 보존, 막힘: `traffic_blocked`·error 이벤트·SSE failed·전환 Activity 없음, 조회 실패: `HANDOFF_UNAVAILABLE`), 티켓 50 전 이력 재생, phase 변경 뒤 `db_gate_ok` 보존(수정 전 코드에서 실패 확인).
- 기존 테스트 조정: `tests/test_migration_plan_workflow.py`·`tests/test_cloud_workload_workflow.py`의 가짜 Provisioning worker에 `get_traffic_handover` 가짜를 추가했다(실행 성공 경로가 이제 이 Activity를 부른다). 클라우드 테스트는 워크로드 Revision으로 조회함을 확인한다. 기존 22 이력(`migration-aws-infra-only-v1.json`) 재생도 통과.
- `make check`: 결과는 PR 본문에 기록.
- HTTP 변경 없음(`make openapi` 불필요). DB 변경 없음(Alembic 없음).

## 미검증
- `make smoke-temporal`(로컬 Temporal compose) 미실행. 같은 경로를 time-skipping 테스트 서버와 재생으로 확인했다.
- 실제 중계 서버·Envoy·`TrafficShiftWorkflow` 자식 연결(#128), 실제 ON_PREM 터널 위 전환, 실제 EKS/GKE LB 주소.

## 협의 필요
- 이상헌님(#128)
  1. 인계 조회는 `get_traffic_handover({plan_id, revision})`(Provisioning 큐)로 확정. 상위 Workflow 상태 `self.traffic_handoff["handover"]`를 `TrafficShiftWorkflow` 요청의 `handover`로 그대로 넘기면 된다. `traffic_blocked` 분기는 이미 있으므로 #128은 `shift_traffic` scaffold 호출 자리를 자식 Workflow로 바꾸고 결과(DONE/ROLLED_BACK/BLOCKED/FAILED)를 반영하면 된다(새 patch 필요).
  2. `cmp_traffic.handover.handover_from_result`는 저장된 결과를 읽어 ON_PREM 목적지가 비고 `blocked_by`가 빠진다. 상위 Workflow 연결에는 쓰지 않기를 제안한다(테스트·실험용 유지 여부는 이상헌님 판단).
  3. `Handover`에 필드를 추가·변경하면 `cmp_contracts.handoff.TrafficHandover`와 계약 테스트(`test_the_handover_model_mirrors_the_traffic_model_field_for_field`)가 실패해 알린다. `INFRASTRUCTURE_READY`는 UNKNOWN으로 보낸다(이유는 `blocked_by`). `Handover`에 `blocked_by`를 받는 자리를 둘지 결정 부탁.
  4. ON_PREM 터널 확인 전(`EXTERNAL_ENDPOINT_UNAVAILABLE`·`TUNNEL_STATUS_STALE`)에 바로 막을지, 일정 시간 재조회할지 정책.
- 데이터 담당(soulee-dev)
  1. DATA_MIGRATION 뷰 필드는 그대로이고 실제 `target_endpoint` 계약 테스트로 고정했다.
  2. #138 병합에서 `phase` setter가 `data_migration`을 지우던 문제를 고쳤다(H8). `db_gate_ok()`가 이제 traffic 단계에서도 값을 가진다.
  3. CDC 실행 위치가 대상 클러스터 밖으로 바뀌어 대상 DB 외부 주소가 필요해지면 필드 이름·노출 범위를 함께 정한다(A-14).

## 문서
- `specs/provisioning/shared-contracts.md`: §Provisioning 결과 인계에 조회 방식·대응표
- `specs/provisioning/plan.md` §11, `specs/provisioning/migration-events.md` E18 보강, `docs/architecture/temporal-flow.md`
- `tickets/provisioning/49-failure-visibility.md`: 후속 링크
- `specs/provisioning/tasks.md`: 50 행

## 완료 체크리스트
- [x] RED → GREEN 테스트
- [x] `make check`
- [x] Temporal 테스트 서버 통합 테스트·재생(이 티켓 전 이력 포함)
- [x] SDD 문서 갱신
- [ ] 이상헌님 확인, 조율 세션 리뷰
