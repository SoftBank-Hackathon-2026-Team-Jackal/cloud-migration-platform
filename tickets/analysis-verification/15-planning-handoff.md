# Ticket 15 — Planning 인계(SandboxResult v2)

## 상태
DRAFT — 사용자 검토 전

## 목적
검증 결과를 Planning 입력 계약 v2로 넘기고, 같은 테스트 묶음을 Production 준비 검사에서 다시 돌릴 수 있게 한다.

## 관련 요구사항
AV-018

## 선행 작업
Blocked by: 14, Provisioning Planning v2 합의

## 작업 범위
- 포함: `SandboxVerification` → Planning `sandbox`·`build` 투영, 대조 규칙 테스트(Digest·소스·요구사항·템플릿 버전), 검증 후 대상·요구사항 변경 시 무효 처리, `test_suite_ref` 재실행 인터페이스
- 제외: Planning·Preview 구현(민서님)

## 수용 기준
- [ ] 투영 결과가 Planning v2 입력 검증을 통과한다.
- [ ] Build Digest와 Sandbox Digest가 다르면 Planning이 거절하는 테스트가 있다.
- [ ] 검증 뒤 대상 선택 버전이 바뀌면 이전 검증을 넘기지 않는다.
- [ ] 테스트 묶음을 다른 `base_url`·인증 참조로 실행하는 예시가 문서화된다.

## 테스트 전략
- 제안 Test Seam: 투영 함수와 민서님 `prepare_plan` v2의 공개 입력 검증. 잡는 것: 계약 정합성. 못 잡는 것: 실제 Production 배포.
- 실행: `uv run pytest tests/sandbox/test_handoff.py tests/test_provisioning.py`

## 완료 시 확인할 동작
통과한 검증 결과로 Planning이 계획 초안을 만든다.

## 결정 대기
- Planning `SandboxResult`/`BuildResult` v2 필드(민서님)
- `*_ref` 저장 위치(S3 vs 업무 DB)

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
