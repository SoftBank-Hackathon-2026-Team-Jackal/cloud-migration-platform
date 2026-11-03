# Ticket 05 — AWS EKS Terraform Plan과 Preview

## 상태
DONE — Issue #2 재사용; 저장 Plan→민감정보 제거 Preview를 실제 AWS Provider로 검증했다. 로컬 State 검증 Plan은 운영 승인 불가.

## 목적
AWS 요구사항을 실제 생성 없이 EKS 네트워크·클러스터 변경 Preview로 제시한다.

## 관련 요구사항
PROV-002, PROV-003, PROV-007, PROV-014, PROV-021

## 선행 작업
Blocked by: 01A, CP-01

## 작업 범위
- 포함: VPC/Subnet/Route/보안·EKS/Node 입력 변환, Terraform fmt/validate/저장 Plan과 민감정보 제거 요약을 구현한다.
- 제외: Terraform Apply, Helm 배포, 승인 API

## 수용 기준
- [x] 승인 전 AWS 자원 변경 없이 검증 가능한 Plan·요약을 만든다.
- [x] 예상 네트워크·EKS·Node·외부 접근과 적용 기본값을 Preview에서 식별한다.
- [x] Plan ID/Revision·템플릿·산출물 Digest를 기록하고 Credential/Secret 원문은 가린다.

## 테스트 전략
- 제안 Test Seam: 저장 Plan 메타데이터와 사용자 Preview. Plan만으로 실제 생성 성공은 입증하지 못한다.
- 확정 Test Seam: 저장된 binary Plan의 바이트 Digest 및 `terraform show -json`에서 허용 필드만 추출한 Preview를 대조한다. 실패·민감값이 출력되지 않는지도 검사한다. 실제 AWS 생성 가능성과 Controller/CSI 준비는 증명하지 않는다.
- 실행: `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`; 격리 계정 Plan은 별도 실행.

## 완료 시 확인할 동작
AWS 대상 Preview와 내부 Plan 참조를 대조할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 기존 네트워크 재사용은 P0 제외, 중앙 S3 State+잠금 채택. 신규 네트워크·Node Profile, 버전/Backend 실제 설정, 격리 계정·권한을 확보해야 실제 Plan 검증이 완결된다.
- 구현 중 변경: AWS 전용 명시 입력·고정 Terraform root, S3 Backend lockfile, 저장 Plan/Spec·템플릿/lockfile Digest, 허용 목록 기반 Preview를 추가했다. 단일 NAT와 ALB 미배포를 경고로 노출한다. 공통 `shared_deployment` 계약은 변경하지 않았다.
- 2026-11-04 완료 기준 조정: 검증 전용 로컬 State Plan은 `LOCAL_STATE_VALIDATION_ONLY` 미해결 결정으로 운영 승인을 차단한다. 중앙 S3 State·잠금과 최종 저장 Plan의 실환경 검증은 첫 실제 승인 전 필수이며, 로컬 검증 Plan을 Apply에 재사용하지 않는다. [ADR-002](../../docs/decisions/ADR-002.md)를 따른다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 로컬 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## 2026-11-04 검증 기록

- Issue [#2](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/2)를 재사용한다. 이슈의 다른 대상 인프라 범위는 이 PR로 닫지 않는다.
- `terraform fmt -check`, `terraform init -backend=false`, `terraform validate` 통과. 로컬 대체 도구로 저장 Plan/Spec Digest, Account mismatch 차단, 민감값 제거 Preview, 불변 Revision 및 Apply 미실행을 검사했다. 최신 main 기준 `make check`: 259 passed.
- `cmp-aws-prod` STS 계정 ID `714066696240` 확인 후, 보호된 임시 디렉터리에서 S3 Backend 블록만 제거한 검증용 복사본으로 AWS Provider 6.71.0·Terraform 1.17.0의 실제 저장 Plan을 실행했다. EKS 1.36·`t3.large`가 대상 리전/선택 AZ에서 제공됨을 읽기 전용으로 확인했다. Plan SHA-256은 `67ff6e899ebb8329a4bc92dfee2f2a66e08ba9498f9c2e79305713d897e03d0d`; 신규 VPC 1, Subnet 4, Route 2, EKS 1, Node Group 1을 포함하며 허용 목록 Preview 변환이 통과했다. 저장 Plan 원문·State·실제 Secret은 커밋하지 않았다. Terraform Apply와 AWS 대상 자원 생성은 실행하지 않았다.
- 실제 공개 CLI를 `--local-validation`으로 끝까지 실행해 저장 Plan/Spec 바이트·Manifest/Preview Digest 일치, 26개 계획 자원 동작, 파일 권한 0700/0600, `LOCAL_STATE_VALIDATION_ONLY` 승인 차단을 확인했다. 저장 Plan SHA-256은 `ee1f04038f68fdf2fe19e9262ca6625452d7e25b16e51664d66ee52201f67e2d`다. 실제 Provider Plan의 자원 종류·행위만 남긴 fixture와 가짜 민감값 제거 테스트도 추가했다. 최신 main 기준 `make check`: 260 passed.
- 이 계정에 S3 버킷이 없어 중앙 S3 State/잠금 경로는 **미검증**이다. 첫 실제 승인 전에 대상 계정·버킷·권한과 S3-backed 최종 Plan/Preview를 검증해야 한다. `ProvisioningSpecV2`에는 아직 구체 AWS Network/Node 필드가 없으므로 이번 AWS 전용 입력은 상위 Activity가 검증된 대상 정책에서 구성해야 하며 자동 변환은 미연결이다. ALB Controller/Ingress·CSI와 앱/DB는 후속 티켓 범위다.

## 순차 작업 조건 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)와 [공유 계약](../../specs/provisioning/shared-contracts.md)을 따른다. 사용자 후속 지시에 따라 한 티켓씩 순차 구현하고 관련 문서·SDD/index 수정은 동일 구현 이슈/PR에 포함한다. 공통 계약을 복제하지 않고 기존 모델을 사용한다.

공통 Preview 계약을 소비하되 이번 티켓의 인프라 Plan만으로 앱/DB 배포 전체를 승인 가능한 상태로 표시하지 않는다. 실제 저장 Plan 검증 미실행 시 해당 수용 기준을 미완료로 남긴다.
