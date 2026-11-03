# Ticket 06 — GCP GKE Terraform Plan과 Preview

## 상태
BLOCKED — 코드·로컬 저장 Plan 검증 완료; 실제 GCP ADC/중앙 S3 State 검증 대기. Issue #2에서 추적

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
- [x] AWS Subnet/AZ 구조를 복사하지 않고 GCP 구성으로 변환한다.
- [x] 승인 전 GCP 자원을 변경하지 않고 검증용 저장 Plan·Preview를 제공한다.
- [x] Plan ID/Revision·산출물 Digest와 Secret이 제거된 Preview를 제공한다.
- [ ] 실제 GCP 프로젝트/중앙 S3 State의 최종 Plan·권한을 검증한다. 로컬 State Plan은 승인 불가다.

## 테스트 전략
- 확정 Test Seam: 저장 Plan 바이트·Manifest Digest와 허용 목록 Preview를 대조하고 로컬 State 계획은 공통 승인 검사에서 거절한다. 실제 GKE 준비 상태는 확인하지 못한다.
- 실행: `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`, 가상 프로젝트의 실제 Provider 저장 Plan→Preview. 실제 프로젝트/S3 Plan은 ADC와 사전 준비 State가 갖춰진 뒤 별도 검증한다.
- 2026-11-04 기준: 실제 GCP Provider의 저장 Plan·민감정보 제거 Preview를 보호된 로컬 State에서 검증할 수 있다. 이 검증 Plan은 승인 불가로 표시한다. 운영 승인은 중앙 S3 State와 잠금을 사용한 새 최종 Plan·Preview 및 대상 프로젝트/ADC 권한 확인이 선행되어야 한다. Mock fixture만으로 DONE 처리하지 않는다.

## 완료 시 확인할 동작
GCP 대상 Preview와 내부 Plan 참조를 대조할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 기존 네트워크 재사용은 P0 제외, 중앙 S3 State+잠금 채택. 신규 네트워크·Node Profile, 버전/Backend 실제 설정, 격리 프로젝트·권한이 필요하다.
- 구현 중 변경: GCP 전용 명시 입력과 Provider 7.x 잠금, S3 lockfile, 저장 Plan/Spec·템플릿 Digest 및 허용 목록 Preview를 추가했다. 리전 Subnet의 Pod/Service 보조 범위와 control-plane /28을 분리하고 Cloud NAT/내부 firewall·기존 node SA 조건을 표시한다. 공통 Preview 계약은 변경하지 않았다.
- 2026-11-04 검증 경로 조정: 사용자 합의에 따라 검증 전용 로컬 State의 실제 Provider 저장 Plan은 허용하되 `LOCAL_STATE_UNAPPROVABLE` 오류로 승인 차단한다. 중앙 S3 State·잠금과 대상 프로젝트 권한 검증은 첫 실제 승인 전 필수다. 이 로컬 Plan은 Apply에 사용할 수 없다. Ticket 05 PR의 [ADR-002](../../docs/decisions/ADR-002.md) 수정과 정합성을 맞춘다.

## 완료 체크리스트
- [x] 로컬 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
- [ ] 실제 GCP ADC·프로젝트/S3 State Plan 검증

## 2026-11-04 검증 기록

- Issue [#2](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/2)를 재사용한다. 05와 다른 대상 인프라·후속 Apply 범위는 이 작업으로 닫지 않는다.
- Terraform 1.10.5, Google Provider 7.46.2로 `terraform fmt -check`, `terraform init -backend=false`, `terraform validate` 통과. Ticket 05 병합 후 `make check`: 265 passed.
- `cmp-plan-local` 가상 프로젝트·무효 토큰·임시 로컬 State에서 공개 CLI로 실제 저장 Plan→Preview를 끝까지 실행했다. VPC Network·regional subnet·Cloud Router/NAT·internal firewall·GKE cluster·Node Pool 각 1개 CREATE를 확인했다. Plan은 7112 bytes, SHA-256 `bf02de6cda9a8a1b7037c1c993822c7a0f067fed538d9902b2e67cf94d3808d6`이다. Spec/Plan ArtifactRef 바이트와 Manifest/Preview Digest가 일치하고 공통 승인 검사는 `VALIDATION_FAILED`로 차단했다. Plan/State/비밀값 원문은 커밋하지 않았다. Apply·실제 GCP 자원 변경은 수행하지 않았다.
- `cmp-gcp-prod` 프로젝트는 `sbh26-prod`로 확인했다. 별도 ADC는 현재 없어 `gcloud auth application-default print-access-token`이 실패한다. 실제 대상 프로젝트 Provider Plan, 사전 생성 node SA/권한, 중앙 S3 State 버킷/잠금은 **미검증**이다. 이 세 조건을 갖춘 후 운영 Plan/Preview를 다시 만들어야 한다.
- 현행 `ProvisioningSpecV2`에는 구체 GCP Network/Node 필드가 없으므로 이번 전용 입력은 상위 Planning Activity가 검증된 대상 정책에서 구성해야 하며 Workflow 연결은 후속 통합 범위다.

## 순차 작업 조건 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)와 [공유 계약](../../specs/provisioning/shared-contracts.md)을 따른다. 사용자 후속 지시에 따라 한 티켓씩 순차 구현하고 관련 문서·SDD/index 수정은 동일 구현 이슈/PR에 포함한다. 공통 계약을 복제하지 않고 기존 모델을 사용한다.

공통 Preview 계약을 소비하되 이번 티켓의 인프라 Plan만으로 앱/DB 배포 전체를 승인 가능한 상태로 표시하지 않는다. 실제 저장 Plan 검증 미실행 시 해당 수용 기준을 미완료로 남긴다.
