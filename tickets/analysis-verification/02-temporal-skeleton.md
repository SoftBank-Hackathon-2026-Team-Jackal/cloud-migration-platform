# Ticket 02 — Temporal 골격과 Sandbox 자식 Workflow

## 상태
DRAFT — 사용자 검토 전

## 목적
스캐폴드 `analyze_project`를 분석 Activity 묶음으로 바꾸고, Sandbox 검증 루프를 자식 Workflow와 mock Activity로 끝까지 연결한다.

## 관련 요구사항
AV-001, AV-015, AV-016, AV-021

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: 분석 Activity 골격(fixture 반환), 스캔 4종 병렬 실행, `SandboxVerificationWorkflow`(mock 빌드·렌더·적용·테스트·판정), 최대 3회 루프, 사람 검토 Signal 대기, `finally` 정리, 진행 Query, `sandbox` Queue·Worker 등록
- 제외: 실제 스캔·빌드·Helm 적용·LLM

## 수용 기준
- [ ] 스캔 4종이 병렬로 실행되는 것이 이력에서 보인다.
- [ ] mock 판정에 실패 1회를 주입하면 수정 → 재시도 → 통과가 이력에 남는다.
- [ ] 실패를 3회 주입하면 `NEEDS_REVIEW`로 Signal을 기다리고 정리 Activity가 실행된다.
- [ ] Workflow 취소 시에도 정리 Activity가 실행된다.
- [ ] Workflow 코드에 외부 I/O·시각·난수가 없다(Temporal 샌드박스 통과).

## 테스트 전략
- 제안 Test Seam: Temporal 테스트 환경(time skipping)에서 Workflow 결과·이력. 잡는 것: 순서·분기·정리 보장. 못 잡는 것: 실제 외부 작업 시간·실패 형태.
- 실행: `uv run pytest tests/sandbox/test_workflow.py`, `make check`, `make smoke-temporal`

## 완료 시 확인할 동작
로컬 Temporal에서 분석 → Sandbox 루프 → 결과까지 mock으로 한 번에 돈다.

## 결정 대기
- `sandbox` Queue 추가 여부(Worker를 나눌지)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
