# Ticket 14B — 갱신 루프와 부모 종료 상태

## 상태
IN_PROGRESS — 구현 완료(SPEC 경로는 실제 k3d, 나머지 경로는 가짜 Activity), 리뷰 대기, Issue 번호 미기록

## 목적
mock Activity를 실제 Sandbox Activity로 바꾸고, 실패 시 분류·수정·같은 Sandbox 갱신으로 재검증하는 루프를 Workflow에 넣는다. 부모 종료 상태 값을 정리한다.

## 관련 요구사항
AV-015, AV-019, AV-020

## 선행 작업
Blocked by: 14A

## 작업 범위
- 포함: 실제 Sandbox Activity(빌드·준비·적용·갱신·검증·분류·수정·정리) 연결과 신호 수집, 갱신 루프(최대 3회, 초과 시 사람 검토), RENDER 중단 보고, 부모 종료 상태 값 확정(검토 대기는 진행 중 플래그, 종료는 완료·실패·취소), 민서님께 새 값 공유와 SSE 종료 조건 반영 요청
- 제외: 최종 발행·재승인(14C), GitHub 권한, 부모 Workflow가 분석 결과를 Sandbox 자식에 넘기는 입력 연결(별도), 실제 Build 호출 배치(D-12)와 실제 Claude 연결(ADR-004)

## 수용 기준
- [x] 포트 오류 주입 → SPEC → 재렌더·갱신(빌드 없음) → 통과가 이력에 남는다. (실제 k3d)
- [x] intent의 `external_host` 항목 누락 주입 → 출구 차단 → SPEC → intent 수정 → 통과가 이력에 남는다. (가짜 Activity 이력. 분류 규칙은 14A, 차단·기록은 13에서 실제 k3d로 검증)
- [x] 앱 코드 오류 주입 → CODE → 새 묶음·새 Digest → 통과가 이력에 남는다. (가짜 Activity 이력만, 실제 클러스터 미검증)
- [x] 렌더 결과가 요구사항과 다르면 수정 없이 중단되고 보고가 남는다.
- [x] INFRA는 Fixer 없이 처리되고 정리가 실행된다.
- [x] 루프 결과(통과, 사람 검토 대기, RENDER 중단, 실패, 취소)가 정해진 종료 상태로 부모에 반영되고, 검토 대기는 종료 상태가 아니다. 기존 `scaffold_complete`·`cancelled` 동작과 기존 이력 replay는 유지된다.
- [x] 새 종료 상태 값과 부모 변경 범위, replay 결과를 PR 설명에 기록해 민서님께 공유하고 SSE 종료 조건 반영을 요청한다.

## 테스트 전략
- 제안 Test Seam: k3d Sandbox + 가짜 LLM(정해진 수정 반환)으로 Workflow 이력. 잡는 것: 분류·루프·갱신 경로. 못 잡는 것: 실제 모델의 수정 품질.
- 실행: `uv run pytest tests/sandbox/test_fix_loop.py`, `make smoke-temporal`

## 구현 요약
- `SandboxVerificationWorkflow.run`은 문자열이면 기존 scaffold, dict(`SandboxInput`)이면 실제 루프를 탄다(`AnalysisWorkflow`와 같은 방식이라 기존 이력은 그대로). 실제 Activity는 `sbx_` 접두어(`sbx_build`, `sbx_prepare`, `sbx_apply`, `sbx_verify`, `sbx_classify`, `sbx_fix`, `sbx_teardown`)로 `activities/sandbox_loop.py`의 `SandboxLoopActivities`에 있고 `workers/sandbox.create_worker(client, loop)`로 주입한다.
- 루프: 빌드 → 준비 → 반복(적용 → 검증(테스트 데이터 초기화·`run_suite`·`judge`·신호 수집) → 분류). 통과하면 `PASSED`, RENDER는 `STOPPED_RENDER`(수정·재빌드 없음), INFRA는 같은 입력으로 재시도(시도 횟수에 안 넣음, 연속 2회면 사람 검토), SPEC은 요구사항 수정 후 갱신(빌드 없음), CODE·BUILD는 작업 공간 복사본 수정 후 새 빌드·갱신, 3회 초과·Fixer 실패·준비 실패는 정리 후 `NEEDS_REVIEW`. 시도별 결과(`attempts`)가 Query와 결과에 남는다. 어떤 경우에도 `finally`에서 정리를 실행한다.
- 적용은 첫 시도에는 설치·네트워크 규칙·검사·기준선 저장, 이후에는 `update_sandbox`로 바뀐 것만 갱신한다. 첫 시도가 기준선 저장 전에 실패했으면 다음 적용에서 기준선을 만든다.
- 분류 신호는 Pod 이벤트, 앱 로그의 스택이 작업 공간 파일을 가리키는지, Helm 시간 초과, 렌더 필드 비교에서 모은다. 시작하지 못한 앱(준비 검사 실패 + 앱 원인 이벤트)은 INFRA가 아니라 앱 문제로 본다(14A 분류기 보강).
- 부모 종료 상태: 검토 대기는 종료 상태가 아니라 부모 Query의 `review_pending` 플래그(자식이 `sandbox_review_pending` Signal로 알림)이고, Sandbox가 `PASSED`가 아니면 부모는 `failed`로 끝나며 원인은 `failure_code`(`NEEDS_REVIEW`·`STOPPED_RENDER`·`FAILED`)에 남는다. 기존 임시 값 `needs_review`·`stopped_render`는 `sandbox-terminal-v1` 패치로 새 실행에서만 바뀌고, 이전 이력은 이전 값으로 replay된다(`tests/fixtures/analysis/migration-before-14b-stopped-render.json`). 사람 검토 종료는 `resolve_review` Signal(승인이 아닌 확인 표시)이다.

## 부모 변경 범위와 민서님께 공유할 내용
- 변경 파일: `workflows/migration.py` 한 곳(Signal `sandbox_review_pending`, Query 필드 `review_pending`·`failure_code`, 패치 `sandbox-terminal-v1`).
- 종료 상태 값: 완료 `scaffold_complete`(기존), 실패 `failed`(신규, 원인은 `failure_code`), 취소 `cancelled`(기존). `needs_review`·`stopped_render`는 새 실행에서 더 이상 나오지 않는다.
- 요청: `apps/api/src/cmp_api/routes/migrations.py`의 SSE 종료 조건 `{"scaffold_complete", "cancelled"}`에 `failed`를 추가해 주세요. `phase`는 문자열이라 OpenAPI 변경은 없고, 이 저장소의 analysis-verification 작업은 `apps/api`를 수정하지 않았습니다. (기존에 `plan_failed` 등 계획 단계 종료 값도 이 조건에 없으니 함께 봐 주세요.)
- replay: 14B 전 이력(`stopped_render`로 끝난 부모)과 02·02B 이전 이력이 모두 replay된다.
- Worker 배포 영향: 이미 실행 중인 부모는 이전 값으로 끝나고 새로 시작한 실행부터 `failed`가 된다.

## 완료 시 확인할 동작
오류를 넣은 데모 앱이 루프를 거쳐 통과하거나 정해진 종료 상태로 끝난다.

## 검증 증거
- `uv run pytest tests/sandbox/test_fix_loop.py`: 가짜 `sbx_*` Activity로 SPEC·CODE·BUILD·RENDER·INFRA 1회·INFRA 연속 2회·3회 초과·Fixer 실패·준비 실패와 부모 알림을 이력으로 확인.
- 실제 k3d: `uv run pytest tests/integration/sandbox -m k3d -k wrong_port`(약 2분). 잘못된 포트(8080)로 시작한 요구사항 → 1회 시도 SPEC(Helm 시간 초과 + 앱 원인 이벤트) → 가짜 모델이 포트 수정 → 같은 Sandbox를 갱신 → 2회 시도 통과, 빌드 1회, 정리 후 네임스페이스 없음.
- 전체 `uv run pytest` 1082 passed, 7 skipped.

## 한계와 미검증
- CODE·BUILD·INFRA 경로는 가짜 Activity 이력으로만 확인했다. 결함을 일으키는 데모 앱 이미지가 없어 실제 클러스터의 CODE 경로는 16번 이후에 확인한다.
- `make smoke-temporal`은 로컬 Temporal 인프라가 없어 실행하지 못했다(time-skipping 테스트로 대체).
- `MigrationWorkflow`는 아직 분석 결과를 Sandbox 자식에 넘기지 않아(자식에는 문자열만 전달) 실제 루프는 `SandboxVerificationWorkflow`를 dict로 직접 시작해야 돈다. 부모의 입력 연결은 별도 작업이다.
- Build는 주입된 함수(`build`)로만 호출하며 D-12 결정 전이다. 모델 클라이언트(`llm_factory`)도 ADR-004 전이라 주입한다.
- Pod 이벤트·로그 기반 신호는 짧은 규칙이라 다른 실패 양상에서는 SPEC으로 떨어질 수 있다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-05: 기존 티켓 14에서 분리.
- 2026-11-05: 구현. 검토 대기는 종료 상태가 아니라 플래그, 실패는 `failed` 하나로 정리.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
