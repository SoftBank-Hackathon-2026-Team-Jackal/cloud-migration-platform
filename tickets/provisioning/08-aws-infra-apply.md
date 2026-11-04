# Ticket 08 — 승인된 Plan으로 AWS EKS 기반 준비

## 상태
IN_PROGRESS — [Issue #2](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/2) 재사용; 착수 결정 E1~E6, 실제 Apply는 사용자 승인(클러스터는 09를 위해 유지)

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
