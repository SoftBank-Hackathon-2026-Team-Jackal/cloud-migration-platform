# Ticket 08B — 필수 기능 확정과 손실 승인 게이트

## 상태
DRAFT — 사용자 검토 전

## 목적
매핑 뒤 Sandbox 전에, 사용자가 필수 기능을 확정하고 잃는 것(APPROVAL 항목)을 승인하게 한다. "필수 기능을 잃지 않는다"를 사용자가 확인하는 단계다.

## 관련 요구사항
AV-022, AV-008

## 선행 작업
Blocked by: 02, 08

## 작업 범위
- 포함: Mapper의 필수 여부 제안(`required_proposed`·근거), Signal `confirm_requirements`·`approve_losses`, `compute_treatment`(확정 필수 여부로 처리 재계산, 거절 시 필수로 바꿔 재계산), `record_review`(불변 기록, 분석 버전에 묶음), 검토 대기 Query, BLOCK 시 중단·보고, 분석 버전 불일치 Signal 거절, CLI로 Signal 보내는 스크립트
- 제외: 승인 화면(상헌), FastAPI 승인 경로 구현은 상헌님과 합의 후(현재 OpenAPI 없음)

## 수용 기준
- [ ] 매핑이 끝나면 Workflow가 검토 대기로 멈추고, Query가 검토할 항목(필수 여부 제안·잃는 것)을 돌려준다.
- [ ] 확정 전에는 `treatment`가 비어 있고, 확정 후 등급 × 필수 여부 표대로 채워진다.
- [ ] `APPROVAL` 항목을 모두 승인하면 Sandbox로 진행하고, 하나를 거절하면 그 기능이 필수로 바뀌어 다시 계산된다.
- [ ] `BLOCK`이 있으면 Sandbox로 가지 않고 사유와 대안이 보고된다.
- [ ] 다른 분석 버전을 가리키는 Signal은 거절된다.
- [ ] 승인 기록(행위자, 시각, 분석 버전, 항목별 결정)이 남고, 자동 승인 경로가 없다.

## 테스트 전략
- 제안 Test Seam: Temporal 테스트 환경에서 Signal을 보내 Workflow 진행·중단과 기록을 확인. 잡는 것: 대기·재계산·버전 고정. 못 잡는 것: 실제 화면·권한 확인.
- 실행: `uv run pytest tests/analysis/test_review_gate.py`, `make smoke-temporal`

## 완료 시 확인할 동작
PARTIAL 선택 기능이 있는 fixture가 승인 전에는 멈춰 있고, CLI로 승인하면 Sandbox로 넘어간다.

## 결정 대기
- 승인 API 경로와 화면 범위(상헌님과 합의)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 원래 목적(필수 기능 보존을 사용자가 확인) 점검에서 빠진 단계를 보완하려고 신설(spec AV-022).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
