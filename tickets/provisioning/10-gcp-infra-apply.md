# Ticket 10 — 승인된 Plan으로 GCP GKE 기반 준비

## 상태
DRAFT

## 목적
승인된 저장 Plan을 적용하고 GKE 접속·Node Pool·영속 스토리지 선행 조건을 확인한다.

## 관련 요구사항
PROV-008, PROV-015, PROV-016, PROV-017

## 선행 작업
Blocked by: 06, 07

## 작업 범위
- 포함: GCP Terraform Apply/Output, State·잠금·실제 리소스 재조회와 GKE 권한/StorageClass 준비를 구현한다.
- 제외: App·PostgreSQL Helm 배포와 트래픽 전환

## 수용 기준
- [ ] 승인 Revision/Digest와 대상 프로젝트/State가 다르면 Apply하지 않는다.
- [ ] GKE·Node Pool·네트워크와 Kubernetes 접근 상태 및 ID를 기록한다.
- [ ] 부분 실패·Worker 종료 뒤 State/실제 자원을 확인해 중복 생성을 막는다.

## 테스트 전략
- 제안 Test Seam: 승인된 GCP 실행 요청 → 인프라 상태/Output. 앱 헬스는 확인하지 못한다.
- 실행: Plan/State 불일치 및 재조회 테스트; 실제 Apply는 명시된 격리 프로젝트에서만 실행.

## 완료 시 확인할 동작
GKE 기반 준비와 재시도 가능한 상태를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Backend/Lock, Node·네트워크 사양, 사전 구축 여부를 먼저 정한다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
