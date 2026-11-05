# Ticket 08B — 필수 기능 확정과 손실 승인 게이트

## 상태
DONE — PR #80 병합, Issue #78

## 목적
매핑 뒤 Sandbox 전에, 사용자가 필수 기능을 확정하고 잃는 것(APPROVAL 항목)을 승인하게 한다. "필수 기능을 잃지 않는다"를 사용자가 확인하는 단계다.

## 관련 요구사항
AV-022, AV-008

## 선행 작업
Blocked by: 02, 08

## 작업 범위
- 포함: `AnalysisWorkflow` 자식의 Signal `confirm_requirements`·`approve_losses`, `compute_treatment`(확정 필수 여부로 처리 재계산, 거절 시 필수로 바꿔 재계산), `record_review`(불변 기록, 분석 버전에 묶음), 검토 대기 Query, BLOCK 시 중단·보고, 분석 버전 불일치 Signal 거절, 자식 ID `analysis-<migration_id>`에 Signal을 보내는 CLI. 부모 공유 Workflow 파일은 변경하지 않는다.
- 제외: 승인 화면(상헌), FastAPI 승인 경로 구현은 상헌님과 합의 후(현재 OpenAPI 없음)

## 수용 기준
- [x] 매핑이 끝나면 Workflow가 검토 대기로 멈추고, Query가 검토할 항목(필수 여부 제안·잃는 것)을 돌려준다.
- [x] 확정 전에는 `treatment`가 비어 있고, 확정 후 등급 × 필수 여부 표대로 채워진다.
- [x] `APPROVAL` 항목을 모두 승인하면 Sandbox로 진행하고, 하나를 거절하면 그 기능이 필수로 바뀌어 다시 계산된다.
- [x] `BLOCK`이 있으면 Sandbox로 가지 않고 사유와 대안이 보고된다.
- [x] 다른 분석 버전을 가리키는 Signal은 거절된다.
- [x] 재분석은 자식 ID를 유지하고 `analysis_ref` 버전으로 구분한다. 검토 상태나 버전이 바뀐 뒤 들어온 Signal도 자식이 거절한다.
- [x] Signal의 확정·승인 키는 매핑 행 ID다. 해당 분석 버전에 없는 행 ID, 확정이 빠진 행, `APPROVAL`이 아닌 행에 대한 승인은 거절된다.
- [x] 승인 기록(행위자, 시각, 분석 버전, 항목별 결정)이 남고, 자동 승인 경로가 없다.

## 테스트 전략
- 합의된 Test Seam: Temporal 테스트 환경에서 Signal을 보내 Workflow 진행·중단과 기록을 확인. 잡는 것: 대기·재계산·버전 고정. 못 잡는 것: 실제 화면·권한 확인.
- 실행: `uv run pytest tests/analysis/test_review_gate.py`, `make smoke-temporal`

## 완료 시 확인할 동작
PARTIAL 선택 기능이 있는 fixture가 승인 전에는 멈춰 있고, CLI로 승인하면 Sandbox로 넘어간다.

## 검증 증거
- `uv run pytest tests/analysis/test_review_gate_logic.py tests/analysis/test_review_gate.py tests/analysis/test_review_cli.py tests/agents/test_mapping.py`: 순수 상태 기계 17, Temporal 통합 9, CLI 도우미 3, 처리 계산 2개 포함 통과.
  - 상태 기계(`cmp_worker.review_gate.ReviewGate`): 확정 전 처리 비어 있음, 확정 후 등급 × 필수 여부 표대로 계산, 손실이 없으면 승인 단계 생략, 필수인데 옮길 수 없으면 사유·대안과 함께 `BLOCKED`, 손실 거절 시 필수로 바꿔 재계산해 `BLOCKED`, 잘못된 분석 버전·누락·알 수 없는 행·승인 대상 아닌 행·잘못된 값·잘못된 단계의 Signal 거절, 거절 기록에 Signal 내용이 없음.
  - Temporal 통합(`AnalysisWorkflow`): 매핑 뒤 검토 대기와 Query 항목, 확정 → 승인 → 완료와 검토 기록(행위자·시각·분석 버전·항목별 결정), 차단 시 자식이 `ANALYSIS_BLOCKED`로 실패하고 종료 후에도 Query로 사유 조회, 거절 시 차단, 다른 버전 Signal은 거절되고 대기 계속, 30일을 기다려도 자동 승인되지 않음, 매핑이 생기기 전 Signal은 `NOT_READY`로 거절, 매핑 행이 없으면 검토 없이 완료, CLI 도우미가 자식이 해당 Signal을 기다릴 때만 전송.
- 구현: `cmp_worker.review_gate`, `cmp_worker.review_client`(Query 후 Signal), `scripts/review_signal.py`(`state`·`confirm`·`approve`), `AnalysisWorkflow`의 Signal 2개·Query 1개, `record_review` Activity, `cmp_agents.mapping.compute_treatments`, 계약 `ConfirmRequirementsSignal`·`ApproveLossesSignal`.
- Helm이 필요한 테스트 파일을 제외한 `pytest` 전체 통과(이 PC에 Helm이 없어 Helm 테스트는 실행 못 함), `ruff check`·`ruff format --check` 통과.
- 설계 변경: Signal은 호출자에게 오류를 돌려줄 수 없어 "거절"을 거절 기록(Query의 `rejections`)과 무시로 구현했고, `BLOCK`은 부모 파일을 바꾸지 않고 멈추도록 자식을 `ANALYSIS_BLOCKED`로 실패시킨다(부모가 자식 실패를 전파). plan §2.3에 반영했다.

## 한계와 미검증
- 실제 Sandbox 이후 단계와 부모 흐름은 연결하지 않았다. 부모는 자식이 실패하면 전파하는 기존 동작만 쓴다.
- `record_review`는 검토 기록을 만들어 돌려줄 뿐 불변 저장은 하지 않는다(D-02).
- 재분석으로 `analysis_ref` 버전이 올라가는 흐름과 루프 중 바뀐 행만 다시 승인받는 동작은 14에서 이 게이트를 다시 써서 구현한다. 이 티켓은 버전이 다른 Signal 거절과 상태 검증까지다.
- API 권한 확인, 화면, 오류 응답 형식은 API 담당과 합의 후 진행한다. CLI는 `--version`을 필수로 받아 다른 버전에 결정이 들어가지 않게 한다.
- `make smoke-temporal`은 이번에 실행하지 않았다. 기본 scaffold는 매핑 행이 없어 검토를 거치지 않고 완료한다.

## 결정 대기
- 승인 API 경로·오류 응답 형식·화면 범위(상헌님과 합의). API는 자식 검토 상태를 먼저 Query하고, 자식이 없거나 검토 대기가 아니면 Signal 없이 오류를 반환한다. Signal은 자식에 직접 보낸다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 원래 목적(필수 기능 보존을 사용자가 확인) 점검에서 빠진 단계를 보완하려고 신설(spec AV-022).
- 2026-11-04(01 리뷰 반영): 확정·승인 키를 매핑 행 ID로 바꾸고(01), 행 ID 대조를 수용 기준에 추가.
- 2026-11-04(전체 재검토): 필수 여부 제안은 08 범위라 포함 목록에서 제거(중복).
- 2026-11-04(사용자 결정): 검토 Signal·Query를 분석 자식에 두고 API는 상태 확인 후 자식에 직접 전달한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
