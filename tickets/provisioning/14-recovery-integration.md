# Ticket 14 — 실패 복구와 세 대상 통합 검증

## 상태
DRAFT

## 목적
세 Provider 경로의 재시도·부분 실패·취소를 검증하고 안전한 결과/복구 상태를 남긴다.

## 관련 요구사항
PROV-015, PROV-016, PROV-017, PROV-018, PROV-019, PROV-021

## 선행 작업
Blocked by: 08, 09, 10, 11, 12, 13

## 작업 범위
- 포함: Terraform/Helm/Agent 중복 실행 방지와 Worker 종료 후 상태 조회, 승인 재검증, 세 대상 통합 시나리오를 구현한다.
- 제외: 무조건 Destroy, DB/트래픽 롤백의 자동화

## 수용 기준
- [ ] Apply 성공 후 응답 유실·Helm 성공 후 Worker 종료·Agent 재연결에서 실제 상태 조회 후 중복 부수효과를 억제한다.
- [ ] 부분 생성 자원과 실패·복구 가능성을 기록하고 취소를 자원 삭제 완료로 표시하지 않는다.
- [ ] EKS/GKE/K3s 각각 검증 증거 또는 미검증 사유를 남기고 필수 검사 실패 시 다음 단계로 가지 않는다.

## 테스트 전략
- 제안 Test Seam: Migration Workflow 실행/재시작 → 외부 상태/Result. 실제 데이터·트래픽 롤백은 확인하지 못한다.
- 실행: Temporal 실패 주입 + 격리 대상 재시도 통합 테스트; 승인된 클라우드 E2E는 별도 실행.

## 완료 시 확인할 동작
세 대상의 중복 실행·부분 실패·취소 처리가 결과와 증거로 설명된다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 자동 정리 범위, 취소 후 보존, Retry/Heartbeat 수치와 실제 Cloud E2E 범위를 결정한다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
