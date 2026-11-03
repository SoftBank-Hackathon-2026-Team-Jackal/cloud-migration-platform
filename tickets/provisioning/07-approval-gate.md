# Ticket 07 — Plan Revision 승인과 Temporal 실행 차단

## 상태
TODO — 범위 검토 완료; CP-01 및 아래 착수 조건 충족 후 구현

## 목적
승인된 정확한 계획만 실행 경계까지 도달하게 한다.

## 관련 요구사항
PROV-014, PROV-015, PROV-016, PROV-021

## 선행 작업
Blocked by: 01, CP-01

## 작업 범위
- 포함: 업무 DB 승인 기록, Plan/Revision/Digest 확인, Temporal 승인 대기·재개와 안전한 가짜 실행 경계를 구현한다.
- 제외: 실제 Terraform·Helm·Agent Apply

## 수용 기준
- [ ] 미승인·오래된 Revision·변경된 Digest·거절 계획은 실행 경계에 도달하지 않는다.
- [ ] 중복 승인과 DB 기록/Signal 전달 재시도로 동일 계획이 중복 실행되지 않는다.
- [ ] 승인 요청 접수와 Workflow 재개 상태를 구분한다.

## 테스트 전략
- 제안 Test Seam: API 승인 요청과 Temporal Workflow 상태/가짜 executor. 실제 인프라 변경은 확인하지 못한다.
- 실행: Temporal 테스트 환경의 승인 대기·중복·stale Revision·거절·재전달 시나리오; HTTP 변경 시 `make openapi`.
- 실제 AWS/GCP 승인 경계는 `LOCAL_STATE_VALIDATION_ONLY` Preview를 거절한다. 대상 계정/프로젝트 및 중앙 S3 State·잠금이 확인된 최종 저장 Plan과 그 Preview/Digest가 일치해야 승인 기록을 만들 수 있다. 가짜 executor 테스트 통과만으로 운영 승인 가능 상태를 선언하지 않는다.

## 완료 시 확인할 동작
Preview ID/Revision을 승인할 때만 안전한 executor가 호출되는 흐름을 볼 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Preview/승인은 유지한다. DB 승인 기록 + Temporal Signal을 사용하고 범용 Outbox는 보류한다. 승인 이벤트 ID 기반 중복 방지·실패 후 재전달·재조회를 구현한다. 제품 API URL·필드·인증/권한·응답 계약은 착수 전에 담당자와 확정한다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 순차 작업 조건 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)와 [공유 계약](../../specs/provisioning/shared-contracts.md)을 따른다. 사용자 후속 지시에 따라 한 티켓씩 순차 구현하고 관련 문서·SDD/index 수정은 동일 구현 이슈/PR에 포함한다. 공통 계약을 복제하지 않고 기존 모델을 사용한다.

필수 시나리오: DB 승인 기록 성공 후 Signal 실패, 같은 승인 재요청/재전달, 승인자 권한 없음, 오래된 Revision/Digest, 실행 전 재확인. 전달 실패를 접수/실행 완료로 숨기지 않는다.
