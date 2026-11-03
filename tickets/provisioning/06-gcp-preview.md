# Ticket 06 — GCP GKE Terraform Plan과 Preview

## 상태
TODO — 범위 검토 완료; CP-01 및 아래 착수 조건 충족 후 구현

## 목적
GCP 요구사항을 실제 생성 없이 GKE 네트워크·클러스터 변경 Preview로 제시한다.

## 관련 요구사항
PROV-002, PROV-003, PROV-008, PROV-014, PROV-021

## 선행 작업
Blocked by: 01, CP-01

## 작업 범위
- 포함: GCP VPC Network/Regional Subnet/Firewall·필요한 NAT/GKE/Node Pool 변환과 저장 Plan·요약을 구현한다.
- 제외: Terraform Apply, Helm 배포, 승인 API

## 수용 기준
- [ ] AWS Subnet/AZ 구조를 복사하지 않고 GCP 구성으로 변환한다.
- [ ] 승인 전 GCP 자원을 변경하지 않고 Plan·검증 결과를 제공한다.
- [ ] Plan ID/Revision·산출물 Digest와 Secret이 제거된 Preview를 제공한다.

## 테스트 전략
- 제안 Test Seam: GCP 저장 Plan 메타데이터와 사용자 Preview. 실제 GKE 준비 상태는 확인하지 못한다.
- 실행: `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`; 격리 프로젝트 Plan은 별도 실행.
- 2026-11-04 기준: 실제 GCP Provider의 저장 Plan·민감정보 제거 Preview를 보호된 로컬 State에서 검증할 수 있다. 이 검증 Plan은 승인 불가로 표시한다. 운영 승인은 중앙 S3 State와 잠금을 사용한 새 최종 Plan·Preview 및 대상 프로젝트/ADC 권한 확인이 선행되어야 한다. Mock fixture만으로 DONE 처리하지 않는다.

## 완료 시 확인할 동작
GCP 대상 Preview와 내부 Plan 참조를 대조할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 기존 네트워크 재사용은 P0 제외, 중앙 S3 State+잠금 채택. 신규 네트워크·Node Profile, 버전/Backend 실제 설정, 격리 프로젝트·권한이 필요하다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 순차 작업 조건 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)와 [공유 계약](../../specs/provisioning/shared-contracts.md)을 따른다. 사용자 후속 지시에 따라 한 티켓씩 순차 구현하고 관련 문서·SDD/index 수정은 동일 구현 이슈/PR에 포함한다. 공통 계약을 복제하지 않고 기존 모델을 사용한다.

공통 Preview 계약을 소비하되 이번 티켓의 인프라 Plan만으로 앱/DB 배포 전체를 승인 가능한 상태로 표시하지 않는다. 실제 저장 Plan 검증 미실행 시 해당 수용 기준을 미완료로 남긴다.
