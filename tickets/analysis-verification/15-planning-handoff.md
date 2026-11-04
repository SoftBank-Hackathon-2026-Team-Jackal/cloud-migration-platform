# Ticket 15 — Planning 인계(입력 v2)

## 상태
DRAFT — 사용자 검토 전

## 목적
분석·빌드·검증 결과를 Planning 입력 v2 형태로 넘기고, 같은 테스트 묶음을 Production에서 다시 돌릴 수 있게 한다.

## 관련 요구사항
AV-018

## 선행 작업
Blocked by: 14, D-02(저장 경계)

## 작업 범위
- 포함: `AnalysisReport`·`RequirementsV2` 바이트·저장된 Build 결과·`SandboxVerification` → `PlanningRequestV2` 투영(`analysis`, `builds[]`, `sandbox`, `environment.mapping_ref`), 검증 후 대상·요구사항 변경 시 무효 처리, `run_suite` 사용 예시 문서
- 제외: Planning·Preview 구현과 대조 규칙(민서님), Production 테스트 실행 시점(D-10)

## 수용 기준
- [ ] 투영 결과가 `prepare_plan_v2(envelope, requirements_bytes=...)` 검증을 통과한다.
- [ ] 리소스별 Build Digest와 `validated_workloads`의 Digest가 같다.
- [ ] 검증 뒤 대상 선택 버전이 바뀌면 이전 검증을 넘기지 않는다.
- [ ] `run_suite`를 다른 `base_url`·인증 참조로 실행하는 예시와, 배포 직후에는 준비 검사만·데이터 이전 후 테스트 묶음 실행이라는 순서가 문서화된다.

## 테스트 전략
- 제안 Test Seam: 투영 함수와 민서님 Planning v2 공개 입력 검증. 잡는 것: 계약 정합성. 못 잡는 것: 실제 Production 배포.
- 실행: `uv run pytest tests/sandbox/test_handoff.py tests/test_provisioning.py`

## 완료 시 확인할 동작
통과한 검증 결과로 Planning이 계획 초안을 만든다.

## 결정 대기
- D-02 `*_ref` 저장 위치·Build 결과 저장 Activity 담당
- D-10 Production 테스트 실행 시점·데이터 정리

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 계약 협의안 v2 형식과 배포 후 테스트 시점 반영. v2 동기화로 투영 대상을 `PlanningRequestV2`·`prepare_plan_v2`로 확정.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
