# Ticket 14B — 갱신 루프와 부모 종료 상태

## 상태
DRAFT — 사용자 검토 전

## 목적
mock Activity를 실제 Sandbox Activity로 바꾸고, 실패 시 분류·수정·같은 Sandbox 갱신으로 재검증하는 루프를 Workflow에 넣는다. 부모 종료 상태 값을 정리한다.

## 관련 요구사항
AV-015, AV-019, AV-020

## 선행 작업
Blocked by: 14A

## 작업 범위
- 포함: Sandbox Activity(준비·적용·갱신·검사·테스트 실행·판정·정리) 연결과 신호 수집, 갱신 루프(최대 3회, 초과 시 사람 검토), RENDER 중단 보고, 부모 종료 상태 값 확정(검토 대기는 진행 중 플래그, 종료는 완료·실패·취소), 민서님께 새 값 공유와 SSE 종료 조건 반영 요청
- 제외: 최종 발행·재승인(14C), GitHub 권한

## 수용 기준
- [ ] 포트 오류 주입 → SPEC → 재렌더·갱신(빌드 없음) → 통과가 이력에 남는다.
- [ ] intent의 `external_host` 항목 누락 주입 → 출구 차단 → SPEC → intent 수정 → 통과가 이력에 남는다.
- [ ] 앱 코드 오류 주입 → CODE → 새 묶음·새 Digest → 통과가 이력에 남는다.
- [ ] 렌더 결과가 요구사항과 다르면 수정 없이 중단되고 보고가 남는다.
- [ ] INFRA는 Fixer 없이 처리되고 정리가 실행된다.
- [ ] 루프 결과(통과, 사람 검토 대기, RENDER 중단, 실패, 취소)가 정해진 종료 상태로 부모에 반영되고, 검토 대기는 종료 상태가 아니다. 기존 `scaffold_complete`·`cancelled` 동작과 기존 이력 replay는 유지된다.
- [ ] 새 종료 상태 값과 부모 변경 범위, replay 결과를 PR 설명에 기록해 민서님께 공유하고 SSE 종료 조건 반영을 요청한다.

## 테스트 전략
- 제안 Test Seam: k3d Sandbox + 가짜 LLM(정해진 수정 반환)으로 Workflow 이력. 잡는 것: 분류·루프·갱신 경로. 못 잡는 것: 실제 모델의 수정 품질.
- 실행: `uv run pytest tests/sandbox/test_fix_loop.py`, `make smoke-temporal`

## 완료 시 확인할 동작
오류를 넣은 데모 앱이 루프를 거쳐 통과하거나 정해진 종료 상태로 끝난다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-05: 기존 티켓 14에서 분리.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
