# Ticket 01 — 분석·검증 계약 v1

## 상태
DRAFT — 사용자 검토 전

## 목적
분석 결과·Sandbox 검증 결과의 계약을 정의하고, 요구사항과 Planning 투영은 main에 구현된 v2 모델(`RequirementsV2`, `PlanningRequestV2`)을 재사용한다.

## 관련 요구사항
AV-001, AV-017, AV-018, AV-020

## 선행 작업
Blocked by: 없음

## 작업 범위
- 포함: `cmp_contracts.analysis`(AnalysisRequest, Evidence, MappingRow(`required_proposed`·`required`·`treatment` 확정 전 null 허용), ReviewRecord, UsageProfile, PrecheckReport, ExternalEntry, EnvExceptions, AnalysisReport)와 `cmp_contracts.verification`(SourceArchive, BuildAttempt, SandboxAttempt, Verdict, SandboxVerification, `PlanningRequestV2` 투영 함수), 요구사항 JSON 정규 직렬화(같은 내용 → 같은 바이트·SHA-256), fixture
- 제외: Provisioning 모델(`provisioning`, `provisioning_v2`, `shared_deployment*`) 수정, 스캐너·Activity 구현, `*_ref` 저장 위치(D-02)

## 수용 기준
- [ ] 엄격한 모델(`extra='forbid'`, 암묵적 변환 금지)이 알 수 없는 필드와 잘못된 Enum을 거절한다. Enum 값은 대문자다.
- [ ] 분석 결과 모델 이름이 `AnalysisReport`이고 Provisioning v1 `AnalysisResult`와 겹치지 않는다.
- [ ] 요구사항을 새로 정의하지 않고 `RequirementsV2`로 만들며, fixture 요구사항 JSON이 `RequirementsV2` 검증을 통과한다.
- [ ] 같은 요구사항을 두 번 직렬화하면 같은 바이트·같은 SHA-256이 나온다.
- [ ] 비밀 항목은 이름만 담고 값 필드가 없다(`EnvExceptions`의 대체 값은 비밀이 아닌 변수만).
- [ ] fixture(EKS → K3s)가 검증을 통과하고 JSON 왕복 후 같다.
- [ ] fixture를 `PlanningRequestV2`로 투영한 결과가 `prepare_plan_v2`(requirements_bytes 포함) 검증을 통과한다.
- [ ] `AnalysisReport`·`EnvExceptions` 형식과 외부 진입 처리(D-15)를 민서님께 **공유**한다. 회신은 완료 조건이 아니다.

## 테스트 전략
- 제안 Test Seam: 공개 모델의 `model_validate`/`model_dump`와 `prepare_plan_v2`를 통한 투영 검증. 잡는 것: 형식·필수값·비밀 필드 유입·v2 대조 규칙. 못 잡는 것: 실제 저장소 조회와 상대 Worker 실행.
- 실행: `uv run pytest tests/analysis/test_contracts.py`, `make check`

## 완료 시 확인할 동작
fixture 하나로 분석 결과·요구사항·검증 결과를 만들고, 투영 결과가 민서님 Planning v2 검증을 통과한다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 계약 협의안 v2(리소스 목록·validated_workloads) 반영, AnalysisReport로 이름 변경, 완료 조건을 '공유까지'로 축소.
- 2026-11-04(v2 동기화): main의 v2 구현(#22, #24, B-01 변경) 기준으로 수정.
- 2026-11-04(목적 점검): 필수 여부 제안·확정 필드와 ReviewRecord 추가(AV-022).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
