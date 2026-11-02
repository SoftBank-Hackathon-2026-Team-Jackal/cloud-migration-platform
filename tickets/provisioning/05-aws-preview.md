# Ticket 05 — AWS EKS Terraform Plan과 Preview

## 상태
TODO — 범위 검토 완료; CP-01 및 아래 착수 조건 충족 후 구현

## 목적
AWS 요구사항을 실제 생성 없이 EKS 네트워크·클러스터 변경 Preview로 제시한다.

## 관련 요구사항
PROV-002, PROV-003, PROV-007, PROV-014, PROV-021

## 선행 작업
Blocked by: 01, CP-01

## 작업 범위
- 포함: VPC/Subnet/Route/보안·EKS/Node 입력 변환, Terraform fmt/validate/저장 Plan과 민감정보 제거 요약을 구현한다.
- 제외: Terraform Apply, Helm 배포, 승인 API

## 수용 기준
- [ ] 승인 전 AWS 자원 변경 없이 검증 가능한 Plan·요약을 만든다.
- [ ] 예상 네트워크·EKS·Node·외부 접근과 적용 기본값을 Preview에서 식별한다.
- [ ] Plan ID/Revision·템플릿·산출물 Digest를 기록하고 Credential/Secret 원문은 가린다.

## 테스트 전략
- 제안 Test Seam: 저장 Plan 메타데이터와 사용자 Preview. Plan만으로 실제 생성 성공은 입증하지 못한다.
- 실행: `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`; 격리 계정 Plan은 별도 실행.

## 완료 시 확인할 동작
AWS 대상 Preview와 내부 Plan 참조를 대조할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 기존 네트워크 재사용은 P0 제외, 중앙 S3 State+잠금 채택. 신규 네트워크·Node Profile, 버전/Backend 실제 설정, 격리 계정·권한을 확보해야 실제 Plan 검증이 완결된다.
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
