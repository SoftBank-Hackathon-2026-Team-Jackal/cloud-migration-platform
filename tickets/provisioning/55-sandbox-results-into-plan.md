# Ticket 55 — 샌드박스 결과(EnvPlan·정리 상태·검증 대상)를 Plan에 반영

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰·병합 대기) — [Issue #190](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/190). 2026-11-06 조율 세션 지시로 착수(3차 병렬 묶음 47·48·53·55·45). 담당 민서(받는 쪽). 생산 쪽(분석 ENV-05·샌드박스 결과)은 박승철님 담당이라 형식을 바꾸지 않았다.

## 배경 (실동작 점검 A-06·A-09·A-10의 민서 부분)
- A-06: 자동 흐름에 EnvPlan을 실을 자리가 없어 `.env` 값이 대상 앱에 가지 않는다.
- A-09: 샌드박스 정리 실패가 Planning·승인 화면에 전해지지 않는다. 조립 함수가 `cleanup_status: "CLEANED"`로 고정한다.
- A-10: "샌드박스가 검증한 대상 = 계획 대상" 검사가 돌지 않는다. 조립 함수가 샌드박스 증거의 대상 ref를 운영자 설정 값으로 채운다.

## 착수 시 다시 확인한 사실 (origin/main `93a18d7`)
- **A-06은 #197(ENV-05·06)에서 이미 이어졌다.** 분석은 `env_file_ref`가 있으면 EnvPlan을 저장하고 `assembly.env_plan_ref`에 남긴다(`analysis_pipeline.py`). `sbx_plan_assemble`은 그 ref를 읽어 `assemble_registration(env_plan=...)`에 넘기고, Planning(ENV-04)은 `<app>-env` Secret과 ON_PREM job `env_secrets`를 만든다. 점검 문서의 xfail(`PlanningSettings`에 `env_plan` 허용)은 44 S12 결정과 맞지 않아 따르지 않는다. 이 티켓은 그 경로를 처음부터 끝까지 묶는 통합 테스트를 더했다(변경 전 코드에서도 통과하는 특성 테스트).
- **A-09의 실제 동작.** 샌드박스 Workflow(`workflows/sandbox.py` `finally`)는 **통과한 실행의 정리가 실패하면 결과를 돌려주지 않고 예외를 낸다.** 그래서 지금 돌아오는 PASSED 결과는 정리를 마친 것이고, `CLEANED`는 현재 생산자에서는 사실과 맞다. 다만 그 사실이 생산자 결과가 아니라 조립 함수의 상수에 있었다. 정리 실패는 Migration 실패로 끝나고 승인 화면에는 오지 않는다.
- **A-10의 실제 동작.** 샌드박스 입력·결과에 대상 선택 ref가 없다. Workflow가 넘길 수 있는 값은 시작 입력의 `planning.target_selection.ref`뿐인데, 이것은 조립 함수가 받는 `settings`와 같은 값이라 비교해도 항상 같다. 받는 쪽만으로는 이 검사를 살릴 수 없다(협의 필요).

## 관련 요구사항
PROV-002, PROV-004, PROV-021. 계약: `specs/provisioning/input-contract-v2.md`(Sandbox `cleanup_status` → `CLEANUP_POLICY_REQUIRED`), `specs/analysis-verification/plan.md` `SandboxVerification`, `specs/env-file-ref/*`.

## 선행 작업
Blocked by: 15(분석·검증), ENV-04, ENV-05·06(#197), 18, 44. 동시 진행: 45(compose, 겹치지 않음), 47·48·53.

## 작업 범위
- 포함
  - 받는 쪽 어댑터 `cmp_worker.sandbox_handoff.sandbox_outcome(result)`: 샌드박스 결과 dict에서 정리 상태와 검증 대상 ref를 읽는다.
  - `assemble_registration(cleanup_status=...)` 인자, `sbx_plan_assemble`이 어댑터 값을 넘김.
  - 통합 테스트: 샌드박스 결과(EnvPlan 포함 fixture) → `sbx_plan_assemble` → `register_planning_input` → 실제 `PlanPublisher`(ON_PREM) → job `env_secrets`·`job_env_secrets`.
- 제외
  - 생산 쪽 형식 변경(샌드박스 결과에 `cleanup_status`·`target_selection_ref` 기록, 정리 실패 시 결과 반환 방식) — 협의 필요(아래)
  - 데모 compose(45), CIDR·DB 이미지 대조(#155·#157)

## 착수 결정 (2026-11-06)

| ID | 결정 | 근거 |
| --- | --- | --- |
| R1 EnvPlan 경로 | 새 경로를 만들지 않는다. 분석 `assembly.env_plan_ref` → `sbx_plan_assemble` → `assemble_registration(env_plan=...)`(44 S12)를 그대로 쓰고 통합 테스트로 고정한다 | #197이 이미 이 경로를 구현했다. `PlanningSettings`는 시작 시점 값이라 EnvPlan(분석 결과)을 담지 않는다 |
| R2 정리 상태 읽기 | `sandbox_outcome`이 결과의 `cleanup_status`(`CLEANED`·`FAILED`·`PENDING`)를 먼저, 없으면 `cleanup_failed`(bool, 샌드박스 `status` 쿼리와 같은 이름)를, 둘 다 없으면 `CLEANED`를 쓴다 | 현재 생산자는 정리 실패 시 결과를 돌려주지 않으므로 필드가 없는 PASSED 결과는 정리된 것이다. 생산자가 필드를 더하면 받는 쪽은 고칠 필요가 없다 |
| R3 정리 실패 표시 | `cleanup_status != CLEANED`는 거절하지 않는다. 등록의 `sandbox.cleanup_status`로 전달해 기존 `prepare_plan_v2`가 Plan Revision에 `CLEANUP_POLICY_REQUIRED` 미해결 결정을 남기고 승인(07)이 막힌다 | `input-contract-v2.md`: 검증 성공이 정리 성공을 뜻하지 않으며 미해결 결정으로 남긴다. Plan 게시 결과(Preview `pending_decisions`)에 드러난다 |
| R4 검증 대상 읽기 | `sandbox_outcome`이 결과의 `target_selection_ref`(`VersionRef`)를 읽어 `expected_target_ref`로 넘긴다(기존 `payload.target_selection_ref`가 있으면 그것이 우선). 다르면 기존 코드 `TARGET_SELECTION_CHANGED`로 인계 거절(Migration `failed`, `failure_code`) | 다른 대상을 가정한 검증 증거로 Plan을 만들면 안 된다(fail-fast). 기존 15의 거절 코드를 재사용한다. 샌드박스가 기록하지 않으면 비교하지 않는다(현재 동작 유지) |
| R5 형식 오류 | 필드가 있는데 형식이 틀리면(`cleanup_status` 미지값, `cleanup_failed` 비bool, `target_selection_ref` 비`VersionRef`) `SANDBOX_RESULT_INVALID`로 인계 거절 | 모르는 값을 `CLEANED`로 바꾸면 A-09가 되풀이된다 |
| R6 Workflow | `MigrationWorkflow._hand_off`는 바꾸지 않는다(patch 없음). 이미 샌드박스 결과 dict 전체(`sandbox`)와 분석 결과(`analysis`)를 `sbx_plan_assemble`에 넘기므로 EnvPlan·정리 상태·검증 대상은 그 안에서 읽는다 | 결정성·Replayer 호환 위험이 없다. Workflow가 따로 넘길 수 있는 대상 ref는 `settings`와 같은 값이라 의미가 없다(위 사실) |
| R7 박승철님 파일 | `planning_handoff.py`는 `cleanup_status` 인자 하나(기본 `CLEANED`, 기존 호출자 바이트 동일), `sandbox_loop.py`는 어댑터 호출과 인자 두 개만 바꿨다. 읽는 규칙은 민서 모듈 `sandbox_handoff.py`에 둔다 | 생산 쪽 형식·동작을 바꾸지 않고 인자 전달만 한다 |

## 수용 기준 (Issue #190)
- [x] 샌드박스 결과의 EnvPlan이 운영 Plan의 `-env` Secret으로 이어진다(통합 테스트). — `app-env` Secret 참조(App Values), job `env_secrets` `[API_TOKEN, APP_MODE]`, `job_env_secrets` 값, EXCLUDED 키 제외, 플랫폼이 쓴 파일에 값 없음.
- [x] 대상 불일치·정리 실패가 Plan 게시 결과에 드러난다. — 정리 실패: Revision Preview `pending_decisions = [CLEANUP_POLICY_REQUIRED]`, 승인 검사 `PENDING_DECISION`. 대상 불일치: 인계 거절 `TARGET_SELECTION_CHANGED`(게시 전에 막음). **단, 현재 샌드박스는 두 필드를 결과에 쓰지 않으므로 실제 흐름에서는 생산자 반영 뒤에 동작한다(협의 필요).**

## Test Seam
`tests/test_sandbox_results_into_plan.py`
- 순수: `sandbox_outcome` — 필드 없음, `cleanup_failed`, `cleanup_status` 우선, `target_selection_ref`, 형식 오류 4종.
- 함수 수준 통합: 생산자 모양 그대로 저장한 분석 `assembly`(`env_plan_ref`)·샌드박스 결과 dict → 실제 `sbx_plan_assemble` → 같은 디렉터리의 플랫폼 저장소로 `register_planning_input` → 실제 `PlanPublisher.publish`(ON_PREM, Helm 렌더) → 승인 → `build_rollout_job`·`dispatch` → `job_env_secrets`(`InMemoryEnvFileStore`).
- Temporal 테스트 서버는 쓰지 않았다. Workflow를 바꾸지 않았고(R6), 결과 dict가 `sbx_plan_assemble`로 가는 것은 기존 `tests/analysis/test_pipeline_wiring.py`가 확인한다.

## 검증
- RED: 변경 전 코드에서 정리 실패·대상 비교·형식 오류 3개 실패, EnvPlan 통합 경로 1개와 나머지는 통과(R1 특성 테스트). 어댑터 모듈이 없을 때는 수집 오류.
- GREEN: `uv run pytest tests/test_sandbox_results_into_plan.py` 11 passed.
- `make check`: 통과(ruff, pytest 2168 passed, 9 skipped, 2026-11-06).
- 점검 A 재현(`a-repro`, 점검 브랜치에만 있음) 중 `test_sandbox_cleanup_status_comes_from_the_sandbox`는 이제 통과한다. `test_hand_off_checks_the_target_the_sandbox_verified`(Workflow 소스에 `target_selection_ref` 문자열)는 R6에 따라 그대로 실패한다. `test_pipeline_can_carry_the_env_plan`은 44 S12와 충돌해 따르지 않는다.

## 협의 필요 (박승철님, 샌드박스 결과 생산 쪽)
1. 정리 실패를 승인 화면에 보이게 하려면, 통과한 실행의 정리가 실패해도 예외 대신 결과를 돌려주고 결과에 `cleanup_status: "FAILED"`(또는 `cleanup_failed: true`)를 넣어야 한다. 받는 쪽은 준비됐다(R2·R3). 지금처럼 Migration을 실패시키는 것이 정책이면 이 항목은 닫는다.
2. 샌드박스가 검증한 대상을 기록하려면 샌드박스 입력에 대상 선택 ref(`planning.target_selection.ref`)를 받아 결과 `target_selection_ref`(`{id, version}`)에 그대로 남기면 된다. 받는 쪽은 준비됐다(R4). 샌드박스 클러스터(k3s `local-path`)가 대상과 다른 경우 무엇을 "검증한 대상"으로 볼지도 정해야 한다.

## 문서 변경
- `specs/provisioning/shared-contracts.md` 55 절, `specs/provisioning/input-contract-v2.md`(정리 상태 출처), `specs/provisioning/tasks.md` 행.
