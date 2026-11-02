# Ticket 08 — 승인된 Plan으로 AWS EKS 기반 준비

## 상태
DRAFT

## 목적
승인된 저장 Plan을 적용하고 EKS 접속·Node·필요한 Controller/CSI 준비 결과를 얻는다.

## 관련 요구사항
PROV-007, PROV-015, PROV-016, PROV-017

## 선행 작업
Blocked by: 05, 07

## 작업 범위
- 포함: AWS Terraform Apply/Output, State·잠금·실제 리소스 재조회, EKS 접근과 Load Balancer Controller/EBS CSI 선행 조건을 다룬다.
- 제외: App·PostgreSQL Helm 배포와 트래픽 전환

## 수용 기준
- [ ] 승인된 Plan과 대상 계정/State가 다르면 Apply하지 않는다.
- [ ] 네트워크/EKS/Node 및 클러스터 접근 상태·리소스 ID를 기록한다.
- [ ] 부분 실패·응답 유실 시 State/실제 자원 재조회 후 중복 생성을 막는다.

## 테스트 전략
- 제안 Test Seam: 승인된 AWS 실행 요청 → 인프라 상태/Output. Helm 워크로드는 확인하지 못한다.
- 실행: 저장 Plan 불일치·State 재조회 통합 테스트; 실제 Apply는 명시된 격리 계정에서만 실행.

## 완료 시 확인할 동작
EKS 인프라 준비 상태와 재시도 가능한 결과를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 실제 클러스터 생성 범위, Terraform Backend/Lock, IAM·Node/네트워크 사양을 먼저 확정한다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## P0 정책 갱신 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)에 따라 AWS/GCP 기존 네트워크 재사용은 제외하고 신규 구성만 실행한다. State는 중앙 S3와 잠금을 사용한다. 실제 리소스 생성은 특정 Revision의 Preview·사용자 승인 이후에만 수행한다.
