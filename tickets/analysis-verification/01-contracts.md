# Ticket 01 — 분석·검증 계약 v1

## 상태
DRAFT — 사용자 검토 전

## 목적
분석 결과와 Sandbox 검증 결과의 경계 전용 Pydantic 계약을 정의하고, 민서님 Planning 입력과 필드 이름을 맞춘다.

## 관련 요구사항
AV-001, AV-017, AV-018, AV-020

## 선행 작업
Blocked by: 없음

## 작업 범위
- 포함: `cmp_contracts.analysis`(AnalysisRequest, UsageProfile, NetworkIntent, Evidence, MappingRow, PrecheckReport, EnvClassification, AnalysisResult)와 `cmp_contracts.verification`(SourceArchive, BuildAttempt, SandboxAttempt, Verdict, SandboxVerification), 데모 앱 기준 fixture JSON, Planning `sandbox.*`·`build.*` 이름 대응표
- 제외: 기존 `cmp_contracts.provisioning` 수정(v2 전환은 15), 스캐너·Activity 구현

## 수용 기준
- [ ] 엄격한 모델(`extra='forbid'`, 암묵적 변환 금지)이 알 수 없는 필드와 잘못된 Enum을 거절한다.
- [ ] 비밀 항목은 이름과 분류(`regenerate`/`share_then_rotate`/`user_input`/`discard`)만 담고 값 필드가 없다.
- [ ] 데모 앱 fixture(EKS → K3s)가 검증을 통과하고 JSON 왕복 후 같다.
- [ ] `SandboxVerification`이 Planning 입력에 필요한 Build 참조·Digest·플랫폼·소스·설정·요구사항·템플릿 버전·테스트 묶음·정리 상태를 모두 가진다.
- [ ] 필드 이름 대응표를 민서님께 공유하고 회신을 이 티켓에 기록한다.

## 테스트 전략
- 제안 Test Seam: 공개 모델의 `model_validate`/`model_dump`. 잡는 것: 형식·필수값·비밀 필드 유입. 못 잡는 것: 상대 Worker 실제 출력과의 일치.
- 실행: `uv run pytest tests/analysis/test_contracts.py`, `make check`

## 완료 시 확인할 동작
fixture 하나로 분석 결과와 검증 결과를 읽고, Planning 입력 항목과 1:1로 대조할 수 있다.

## 결정 대기
- Planning 계약 v2 필드 이름(민서님 회신)

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
