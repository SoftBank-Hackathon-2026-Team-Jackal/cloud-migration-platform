# Ticket 08 — 승인된 Plan으로 AWS EKS 기반 준비

## 상태
DONE — [Issue #2](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/2) 재사용; 사용자 승인으로 실제 AWS에 1회 Apply했고 클러스터 `cmp-target-aws`는 09를 위해 유지한다(자동 삭제 없음)

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
- [x] 승인된 Plan과 대상 계정/State가 다르면 Apply하지 않는다.
- [x] 네트워크/EKS/Node 및 클러스터 접근 상태·리소스 ID를 기록한다.
- [x] 부분 실패·응답 유실 시 State/실제 자원 재조회 후 중복 생성을 막는다.

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
- 구현 중 변경: 2026-11-04 착수 결정 E1~E6. 07 실행 경로를 claim/Provider/complete로 분리했고, AWS 템플릿을 `aws-eks-v2`(OIDC·EBS CSI·LB Controller IRSA·Output)로 올렸다. FULL 범위에서는 앞 단계의 참조를 다음 단계에 전달한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰 (자체 리뷰; PR 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록

## P0 정책 갱신 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)에 따라 AWS/GCP 기존 네트워크 재사용은 제외하고 신규 구성만 실행한다. State는 중앙 S3와 잠금을 사용한다. 실제 리소스 생성은 특정 Revision의 Preview·사용자 승인 이후에만 수행한다.

## 착수 결정 (2026-11-04)

사용자 지시에 따라 기획 문서의 최신 결정을 우선 적용하고, 없는 항목은 구현자가 최선안을 정해 기록한다. 실제 AWS Apply(비용·자원 생성)는 별도로 사용자 승인을 받는다.

| ID | 결정 | 근거 |
| --- | --- | --- |
| E1 실행 분기 | 07의 단일 `execute_approved_plan`을 `claim_approved_execution`(DB 재확인 + RUNNING 표식) → `execute_<provider>_plan`(장시간, Heartbeat, 재시도 1회 금지) → `complete_approved_execution`(SUCCEEDED/FAILED/UNKNOWN·리소스 참조 기록)으로 나눈다. Provider Activity 입출력은 작은 식별자·코드·참조만 쓴다. Activity 이름은 (AWS, INFRASTRUCTURE)→`execute_aws_plan`(08), (AWS, WORKLOADS)→`execute_aws_workloads`(09), ON_PREM→`execute_on_prem_plan`(12)이다. 분기 소유는 08이다. | EKS Apply는 수십 분이 걸려 07의 5분 Activity로 감쌀 수 없다. plan.md §10 Heartbeat·재발행 금지. |
| E2 Apply 입력 | 저장된 binary Plan만 Apply한다(재Plan 금지). 실행 전 STS 계정 = manifest `target.account_id`, Backend key `cmp/<environment_id>/terraform.tfstate` = `state_ref`, Plan 바이트 SHA-256·크기 = manifest `terraform_plan` ArtifactRef, Preview에 `LOCAL_STATE_*` 없음을 확인한다. 하나라도 다르면 Apply하지 않는다. | ADR-002 #7·#10, plan.md §8 |
| E3 산출물 위치 | P0는 Worker 설정 `PROVISIONING_ARTIFACT_ROOT`(0700) 아래 05 형식 `<plan_id>/<revision>/`의 저장 Plan을 읽는다. S3 산출물 저장소 연동은 후속. | 05가 같은 디렉터리 형식으로 저장. 단일 EC2 데모 호스트와 호환. |
| E4 중복·복구 | Activity는 재시도하지 않는다. Terraform의 저장 Plan은 State가 바뀌면 stale로 거절되므로 같은 Plan의 두 번째 Apply를 막는다. Apply 결과가 불명확하면(타임아웃·Worker 종료·stale) `UNKNOWN`으로 기록하고, State 목록과 EKS/VPC 실제 상태를 재조회한 결과만 남긴다. 자동 destroy·재Apply는 하지 않는다. | plan.md §8·§12, temporal-flow §10 |
| E5 결과 | Terraform Output(비밀 아님: VPC·Subnet·Cluster 이름/ARN·Node Group·OIDC Provider ARN)과 EKS `describe-cluster`/`describe-nodegroup` ACTIVE 상태를 `resource_refs`로 기록한다. 민감 Output·kubeconfig·토큰은 결과·이력에 넣지 않는다. | PROV-016·021 |
| E6 Controller/CSI 선행 조건 | Terraform 템플릿에 IAM OIDC Provider, EBS CSI 관리형 Add-on + IRSA 역할, AWS Load Balancer Controller용 IRSA 역할·정책을 추가한다. Controller Helm 설치와 StorageClass·Ingress는 09에서 한다. 템플릿 변경이므로 05 Preview 허용 목록을 갱신하고 S3 Backend의 새 Plan·Revision을 만든다. | 08 범위 "LB Controller/EBS CSI 선행 조건" |

### 합의한 Test Seam

- Seam: 승인된 AWS 실행 요청(claim 결과) → `execute_aws_plan` Activity → 인프라 상태/`resource_refs`. Terraform·AWS 호출은 Activity 내부의 실행 포트(명령 러너·STS/EKS 조회)를 가짜로 바꿔 계정·State·Digest 불일치, stale Plan, 타임아웃 후 UNKNOWN/재조회, 성공 시 참조 기록을 확인한다. Workflow 분기는 Temporal 테스트 환경에서 확인한다.
- 놓치는 것: 실제 AWS 권한·할당량·EKS 생성 시간, 실제 Terraform State 잠금 경합. 이는 승인된 격리 실행(아래 실제 검증)에서만 확인한다. Helm 워크로드는 09.

## 검증 기록 (2026-11-04)

| 명령·환경 | 결과 |
| --- | --- |
| `make check` | 322 passed (승인 Workflow 11, AWS Apply 12, Activity 3, Preview 7 포함) |
| `terraform fmt -check` / `init -backend=false` / `validate` (Terraform 1.17.0) | 통과 |
| `scripts/plan_aws_eks.py`, S3 Backend `cmp-demo-714066696240-apne2-state` key `cmp/target-aws-demo/terraform.tfstate` | 최종 저장 Plan `plan-aws-target` r1. CREATE 전용, 오류·미해결 결정 없음 |
| 로컬 업무 DB·Temporal + 승인 HTTP + `ProvisioningApprovalWorkflow` + `execute_aws_plan`(사용자 승인으로 실제 실행) | 승인 201 `DELIVERED` → Workflow `EXECUTED`/`TERRAFORM_APPLIED` → 실행 표식 `SUCCEEDED` → 완료 후 재전달 200, 추가 실행 없음 |
| 읽기 전용 확인 (`cmp-aws-prod`, 계정 714066696240, ap-northeast-2) | EKS `cmp-target-aws` 1.36 ACTIVE, Node Group `cmp-target-aws-primary` ACTIVE, 노드 2개 Ready(v1.36.5), `aws-ebs-csi-driver` ACTIVE, kube-system Pod 실행 중, S3 State 객체 존재 |

기록한 참조: `vpc-09c2dcea1ca1cda3a`, 공개·비공개 Subnet 각 2개, 클러스터 ARN, OIDC Provider ARN, EBS CSI·LB Controller IRSA 역할 ARN. 기존 `cmp-demo-source`는 변경하지 않았다.

### 미검증·제한 사항

- 실제 Apply 중 부분 실패·응답 유실·stale Plan은 가짜 러너로만 확인했다. 실서버에서 장애를 주입하지 않았다.
- `UNKNOWN` 이후 실제 상태를 조회해 재개하는 운영 절차(사람 조치)는 아직 자동화하지 않았다.
- 클러스터 인증 모드는 기본값 `CONFIG_MAP`이다. Access Entry가 없고, 생성 주체(`cmp-aws-prod`)만 cluster-admin이다. 운영 접근 모델은 후속 결정이다.
- Plan 산출물은 로컬 보호 디렉터리(`PROVISIONING_ARTIFACT_ROOT`)에만 있다. S3 산출물 저장소 연동은 후속이다.
- 실행 중인 자원은 비용이 발생한다(EKS·NAT·`t3.medium` 2대). 정리는 09 이후 사용자가 결정하며, 자동 destroy는 없다.
- `spec.md`의 요구사항은 바뀌지 않았다.
