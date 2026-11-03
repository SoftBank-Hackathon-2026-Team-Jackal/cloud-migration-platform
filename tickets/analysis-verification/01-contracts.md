# Ticket 01 — 분석·검증 계약 v1

## 상태
DRAFT — 사용자 검토 전

## 목적
분석 결과·요구사항·Sandbox 검증 결과의 경계 전용 Pydantic 계약을 정의하고, 민서님 Planning 입력 v2와 대응표를 만들어 공유한다.

## 관련 요구사항
AV-001, AV-017, AV-018, AV-020

## 선행 작업
Blocked by: 없음

## 작업 범위
- 포함: `cmp_contracts.analysis`(AnalysisRequest, Evidence, MappingRow, UsageProfile, PrecheckReport, NetworkIntent, ResourceRequirement(APPLICATION/POSTGRESQL), Requirements, EnvExceptions, AnalysisReport)와 `cmp_contracts.verification`(SourceArchive, BuildAttempt, SandboxAttempt, Verdict, ValidatedWorkload, SandboxVerification), 데모 앱 기준 fixture JSON, Planning 입력 v2 대응표
- 제외: 기존 `cmp_contracts.provisioning` 수정(v2 전환은 15), 스캐너·Activity 구현, 민서님 회신 대기(D-02로 분리)

## 수용 기준
- [ ] 엄격한 모델(`extra='forbid'`, 암묵적 변환 금지)이 알 수 없는 필드와 잘못된 Enum을 거절한다. Enum 값은 대문자다.
- [ ] 분석 결과 모델 이름이 `AnalysisReport`이고 Provisioning v1 `AnalysisResult`와 겹치지 않는다.
- [ ] 요구사항이 `resources[]`(id·kind)와 `{resource_id, output}` 참조로 표현되고, 없는 ID를 참조하면 거절된다.
- [ ] 비밀 항목은 이름만 담고 값 필드가 없다(`EnvExceptions`의 대체 값은 비밀이 아닌 변수만).
- [ ] 데모 앱 fixture(EKS → K3s)가 검증을 통과하고 JSON 왕복 후 같다.
- [ ] Planning 입력 v2(`analysis.*`, `builds[]`, `sandbox.validated_workloads`, `test_suite_ref`, `environment.mapping_ref`) 대응표를 민서님께 **공유**한다. 회신은 D-02에서 추적하며 이 티켓의 완료 조건이 아니다.

## 테스트 전략
- 제안 Test Seam: 공개 모델의 `model_validate`/`model_dump`. 잡는 것: 형식·필수값·ID 참조·비밀 필드 유입. 못 잡는 것: 상대 Worker 실제 출력과의 일치.
- 실행: `uv run pytest tests/analysis/test_contracts.py`, `make check`

## 완료 시 확인할 동작
fixture 하나로 분석 결과·요구사항·검증 결과를 읽고 Planning 입력 v2 항목과 1:1로 대조할 수 있다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 계약 협의안 v2(리소스 목록·validated_workloads) 반영, AnalysisReport로 이름 변경, 완료 조건을 '공유까지'로 축소.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
