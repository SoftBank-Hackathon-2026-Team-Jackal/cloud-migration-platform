# Ticket 02B — Sandbox 자식 Workflow 골격

## 상태
DRAFT — 사용자 검토 전

## 목적
Sandbox 검증 루프를 자식 Workflow와 mock Activity로 끝까지 연결한다.

## 관련 요구사항
AV-014, AV-015, AV-016, AV-021

## 선행 작업
Blocked by: 02, Q-01

## 작업 범위
- 포함: `SandboxVerificationWorkflow`(mock 빌드·구성·렌더·적용·테스트·판정·분류), 최대 3회 루프, 사람 검토 Signal 대기, `publish_final_report` 자리, `finally` 정리, 진행 Query
- 제외: 실제 빌드·Helm 적용·LLM

## 수용 기준
- [ ] mock 판정에 실패 1회를 주입하면 분류 → 수정 → 재시도 → 통과가 이력에 남는다(SPEC이면 빌드 없이, CODE면 재빌드 경로).
- [ ] 실패를 3회 주입하면 `NEEDS_REVIEW`로 Signal을 기다리고 정리 Activity가 실행된다.
- [ ] RENDER 분류면 수정 없이 중단되고 정리된다.
- [ ] Workflow 취소 시에도 정리 Activity가 실행된다.
- [ ] 진행 Query가 현재 단계·시도 번호·판정을 돌려준다.

## 테스트 전략
- 제안 Test Seam: Temporal 테스트 환경(time skipping)의 결과·이력. 잡는 것: 분기·정리 보장. 못 잡는 것: 실제 외부 작업 시간·실패 형태.
- 실행: `uv run pytest tests/sandbox/test_workflow.py`, `make smoke-temporal`

## 완료 시 확인할 동작
분석 → Sandbox 루프 → 결과까지 mock으로 한 번에 돈다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 기존 02에서 분리.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
