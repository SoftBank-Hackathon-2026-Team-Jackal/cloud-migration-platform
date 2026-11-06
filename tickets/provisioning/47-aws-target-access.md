# Ticket 47 — AWS 대상 구축 권한·교차 계정 ECR·사용자 클러스터 접근 (23·28·29·46 후속)

## 상태
IN_PROGRESS — [Issue #182](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/182). 로컬 구현·검증 완료. 대상 역할 템플릿 v2는 오프라인 평가와 IAM `simulate-custom-policy`(읽기 전용)로 확인했다. 빌드 스택 변경은 변경 세트까지만 만들었다. 스택 실행·템플릿 v2 업로드·서버 `.env` 반영, 대상 역할로 한 실제 Apply·교차 계정 pull 실측은 승인 대기다.

## 배경 (점검 B-04·D-06·B-08·B-09·B-10·D-10·B-21·B-14, P2 B-15·B-18·B-19·B-20, 41 결정 D4)
- 46(#207)과 23b(#208) 뒤로 AWS 대상 Plan·Apply·워크로드는 사용자 **대상 연결 역할**(`infra/connections/target-role.yaml`)로 실행된다. 그러므로 대상 계정 권한의 실제 주체는 이 템플릿이다. 그런데 그 권한이 Terraform·kubectl의 실제 호출을 덮는지 확인한 적이 없었다(B-04·D-06·B-14). 템플릿 머리말도 "Real Apply with this role is unverified"였다.
- 플랫폼 ECR `cmp-platform-builds`에 저장소 정책이 없어 다른 계정 EKS 노드가 이미지를 받지 못한다(B-08).
- K3s·GKE에 주는 ECR 토큰이 인스턴스 역할 권한 그대로라 push와 저장소 전체 pull이 가능하다(B-09). worker의 GKE 토큰은 `PROVISIONING_AWS_PROFILE`로 발급된다(41 D4).
- 새 EKS에 사용자 kubectl 접근을 만드는 단계가 없다(B-10).
- 클러스터 API 접근 CIDR에 플랫폼 egress IP가 들어 있는지 검사하지 않는다(D-10·B-21).
- P2: SCP가 서울 리전만 허용(B-15), 대상 역할 읽기 권한 과다(B-18), 역할 하나가 모든 일(B-19), worker 전역 `AWS_PROFILE`(B-20).

## 관련 요구사항
PROV-003, PROV-007, PROV-010, PROV-013, PROV-018, PROV-021

## 선행 작업
Blocked by: 23(#118), 28(#152), 29(#153), 46(#181, main 반영). 23b(#205)의 `ConnectionAssumePolicy`·템플릿 버킷은 AWS에 반영돼 있다.

## 작업 범위
- 포함: 대상 역할 템플릿 권한 대조·수정(v2), Terraform access entry·SG 규칙 태그, Plan 입력 `cluster_admin_principal_arns`와 인계 출력, Plan의 플랫폼 egress 검사, 빌드 스택 ECR 저장소 정책·pull 전용 역할, api·worker pull 토큰 프로필, Build 모듈 프로필 명시와 worker 전역 `AWS_PROFILE` 제거, 문서, 변경 세트.
- 제외: GCP(48), destroy·Apply 타임아웃(53), compose의 다른 데모 배선(45), 원격 변경 전부(아래 승인 대기).

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 대상 권한 주체 | 대상 구축 권한은 **사용자 대상 연결 역할에만** 둔다. 인스턴스 역할(연결 없는 고정 프로필 경로, B-04 나머지)에는 EC2·EKS·IAM 권한을 주지 않는다. 데모 같은 계정도 플랫폼 계정에 `target-role.yaml` v2 스택을 만들어 연결로 등록한다 | B-19: 인스턴스 역할은 이미 빌드·State·연결 Assume을 맡는다. 같은 템플릿 하나로 검증 범위가 하나가 된다 |
| D2 호출 목록과 검증 | 필요한 호출을 코드로 고정한다: `cmp_providers.aws_target_access.TERRAFORM_CALLS`(Terraform 리소스 타입별 생성·조회·변경·삭제, AWS Provider 6.71)·`WORKLOAD_CALLS`·`refusals`. 각 호출은 SAR(Service Authorization Reference JSON)의 인가 리소스와 태그 키를 따른다. 테스트가 템플릿을 오프라인 평가하고, `infra/connections/simulate-target-role.py`가 같은 질문을 IAM `simulate-custom-policy`에 묻는다 | 시뮬레이터는 AWS가 새 리소스에 어떤 키를 보내는지 재현하지 못한다. 그래서 EC2는 엄격 해석(부모 리소스에는 요청 태그 없음)으로 질문하고, 두 해석 모두 통과하게 문장을 나눴다 |
| D3 EC2 생성 (B-14) | 새 리소스는 `aws:RequestTag/cmp:managed-by`, 부모(VPC·서브넷·EIP)는 `aws:ResourceTag`를 따로 요구한다(`CreateTaggedNetwork`·`CreateTaggedInVpc`·`CreateTaggedNatGateway`·`CreateInTaggedParents`). SG 규칙·ENI 하위 리소스는 무조건 허용하되 같은 호출이 태그 있는 SG·EIP를 요구한다(`SubResourcesOfTagged`). Terraform SG 아웃바운드 규칙에 `tags = local.tags` | v1은 엄격 해석에서 `CreateSubnet`·`CreateRouteTable`·`CreateSecurityGroup`·`CreateNatGateway`·`AuthorizeSecurityGroupEgress`(규칙 리소스)가 거부된다(시뮬레이션 24건 불일치, 아래 검증) |
| D4 태그 부여 (B-18) | `ec2:CreateTags`는 생성 호출 안에서만(`ec2:CreateAction` 9종) 또는 이미 태그 있는 리소스에만. `eks:TagResource`는 `cmp-` 이름 EKS 리소스에만 | v1은 요청 태그만 맞으면 **사용자 기존 VPC에 플랫폼 태그를 붙이고 그 뒤 삭제**할 수 있었다(시뮬레이션 `allowed`) |
| D5 EKS 범위 | 쓰기·조회는 `cluster|nodegroup|addon|access-entry/${ResourcePrefix}*` + 쓰기는 리소스 태그. `ResourcePrefix`는 `AllowedValues: [cmp-]`로 고정(46 C15, B-12 템플릿 쪽). 계정 단위 조회(`ListClusters` 등)만 `*` | Plan이 연결 경로 `cluster_name`을 `cmp-`로 강제한다(46). 이름과 태그를 함께 봐서 사용자 클러스터를 건드리지 못한다 |
| D6 IAM 범위 (B-18) | 쓰기·조회 모두 `role|policy/${ResourcePrefix}*`·`oidc-provider/oidc.eks.*`만. `iam:Get*`/`List*` `*` 제거. `AttachRolePolicy`는 `iam:PolicyARN`이 검토한 AWS 관리형 5개(테스트가 Terraform과 대조) 또는 접두어 정책일 때만. 인라인 `PutRolePolicy` 제거. `CreateRole`·`TagRole`에 `cmp:managed-by=cloud-migration-platform` 요청 태그는 명시 거부 | v1은 접두어 역할에 `AdministratorAccess`를 붙이고, 플랫폼 주체(23b 정책: 그 태그 + ExternalId)가 Assume할 수 있는 역할을 만들 수 있었다 |
| D7 읽기 | `ec2:Describe*`·`elasticloadbalancing:Describe*`는 유지(Provider 버전마다 조회가 달라 목록화하면 실제 Apply에서 깨질 위험), 대신 `ec2:DescribeInstanceAttribute`·`DescribeLaunchTemplateVersions`(사용자 데이터) 명시 거부. ELB 조회는 53의 정리 사전 확인용 | 사용자 데이터에 비밀이 있을 수 있다. 나머지 Describe는 메타데이터다 |
| D8 정책 크기 | 렌더링 7,788자 → 관리형 정책 한도 6,144자를 넘어 `TargetBuild`(VPC)·`TargetCluster`(EKS·IAM·거부) 둘로 나눔. 테스트가 크기 검사 | 하나로 두면 스택 생성이 실패한다 |
| D9 템플릿 버전 | `connections/v2`(`deploy-connection-templates.sh version=v2`). 버킷 정책은 현재 버전 접두어만 공개라 v1 객체는 비공개가 된다. 이미 만든 v1 스택은 영향 없고, 갱신하려면 사용자가 스택 템플릿을 v2 URL로 교체 | 46 C15에서 정한 순서. 지금 사용자 스택은 없다(23b 테스트 스택 정리됨) |
| D10 사용자 kubectl (B-10) | Terraform `access_config { authentication_mode = "API", bootstrap_cluster_creator_admin_permissions = true }`. 입력 `cluster_admin_principal_arns`(선택, 최대 10, IAM 역할·사용자, 경로·다른 계정 허용, root·STS 세션·서비스 연결 역할 거절)마다 STANDARD access entry + `AmazonEKSClusterAdminPolicy`(클러스터 범위, `access.tf`). 템플릿 `aws-eks-v4`. 비면 Preview 경고 `NO_USER_CLUSTER_ACCESS`. 인계 표시: Apply 결과 `resource_refs.eks_cluster_admin_principal_arns` | EKS 문서: 역할 ARN은 경로를 가질 수 있고 STANDARD는 다른 계정 주체도 된다. `API_AND_CONFIG_MAP`은 쓰는 곳이 없어 표면만 넓다. 대상 역할 정책은 `eks:accessEntryType=STANDARD`와 관리자 정책만 허용한다 |
| D11 입력 경로 | 새 필드는 기존 `PlanningSettings.target.settings`(자유 dict → `AwsPlanInput`)로 들어온다. HTTP 계약·OpenAPI 변경 없음. 웹 입력 화면·44 카탈로그에 칸을 추가하는 일은 협의 필요 | 기존 저장 spec은 기본값 `[]`로 그대로 읽힌다 |
| D12 플랫폼 egress (D-10·B-21) | worker 설정 `PROVISIONING_PLATFORM_EGRESS_CIDRS`(쉼표 구분 CIDR, 데모 EIP `/32`). 설정되면 Plan이 클라우드 호출 전에 각 egress가 `api_access_cidrs` 중 하나에 포함되는지 검사(`PLATFORM_EGRESS_NOT_ALLOWED`, 설정 형식 오류 `INVALID_PLATFORM_EGRESS`). 비면 거절 대신 Preview 경고 `PLATFORM_EGRESS_UNCHECKED`. 자동 추가는 하지 않는다 | 사용자가 검토한 입력을 바꾸지 않는다. 검사 함수는 공용(`cmp_providers.platform_egress`)이라 GKE(48)가 같은 함수를 쓸 수 있다 |
| D13 교차 계정 ECR (B-08) | 빌드 스택 매개변수 `PullAccountIds`(기본 빈 값 = 정책 없음). 값이 있으면 저장소 정책이 `Principal: "*"` + `aws:PrincipalAccount ∈ 목록` + `aws:PrincipalArn` `arn:aws:iam::*:role/cmp-*-eks-node`에게 pull 세 동작만 허용. 운영자가 `CMP_ECR_PULL_ACCOUNT_IDS`로 바꾸고, 주지 않으면 이전 값 유지 | 계정 목록 없이 이름 패턴만 쓰면 아무 계정이나 `cmp-x-eks-node` 역할을 만들어 다른 고객 이미지를 받을 수 있다. 연결 등록 때 자동 추가는 플랫폼 주체에 `ecr:SetRepositoryPolicy`가 필요해 후속으로 둔다. 대상 계정 복제(ECR replication)는 비용·지연이 커서 제외 |
| D14 pull 전용 토큰 (B-09·41 D4) | 빌드 스택에 이름 고정 역할 `cmp-demo-ecr-pull`(`GetAuthorizationToken` + 이 저장소 pull 세 동작, 인스턴스 역할만 Assume). `ensure-secrets.sh`는 `.env`의 `PLATFORM_ECR_PULL_ROLE_ARN`이 있으면 프로필 `cmp-demo-ecr-pull`(`source_profile = cmp-demo-build`)을 쓴다. api·worker 모두 `PLATFORM_ECR_AWS_PROFILE`(compose 기본 `cmp-demo-build`, `.env`로 전환). worker GKE 토큰은 `PLATFORM_ECR_AWS_PROFILE`, 없으면 `PROVISIONING_AWS_PROFILE` | 반영 순서와 무관하게 깨지지 않는다(역할·`.env`가 생기기 전에는 41 동작). 고객별 저장소 분리는 범위 밖 |
| D15 worker 전역 프로필 (B-20) | compose worker의 `AWS_PROFILE` 제거. Build 모듈 `ImageBuilder(context, aws_profile=)`가 자기 `aws` 호출(S3·ECR)의 환경에만 `AWS_PROFILE`을 준다(`ImageBuildAdapter`가 `BuildSettings.aws_profile` 전달) | Build의 S3 호출에는 `--region`이 없어 프로필의 리전이 필요하다. 전역 값을 빼고 명시 전달로 바꿨다 |
| D16 B-15 | 코드는 바꾸지 않고 README에 "플랫폼 계정 같은 계정 대상은 `ap-northeast-2`만"을 적었다. 입력 검증으로 막는 것은 사용자 계정에 해당하지 않아 하지 않는다 | SCP는 플랫폼 계정에만 걸린다 |
| D17 ECR 토큰 수명 | API 문서 "토큰은 12시간 유효". Assume한 역할(최대 1시간 세션, 역할 체이닝)로 발급한 토큰의 실제 수명은 확인하지 못했다 | K3s는 `expiresAt` 절반에 갱신(29)해 영향이 없다. GKE는 6시간 주기 갱신이라 수명이 짧으면 문제다 → 반영 뒤 `expiresAt` 확인 단계(아래 4)에 넣었다 |

## 함수 경계 (48·53·45와 겹치는 곳)
- `cmp_providers.aws_target_access`(신규): 대상 역할 호출 목록·거부 목록·템플릿 렌더. 53이 destroy 전용 호출(예: 컨트롤러가 만든 LB 정리)을 추가하면 여기와 템플릿을 같이 고친다.
- `cmp_providers.platform_egress.check_platform_egress(api_access_cidrs, platform_egress_cidrs, *, path)`(신규): 48 GKE Plan이 `master_authorized_networks`에 쓸 수 있다.
- `aws_preview.plan_aws_infrastructure(..., platform_egress_cidrs=())`, `AwsPlanInput.cluster_admin_principal_arns`, `TEMPLATE_VERSION = "aws-eks-v4"`.
- `aws_apply.OUTPUT_REFS`에 한 줄(`cluster_admin_principal_arns`)만 추가. destroy·타임아웃(53)은 건드리지 않았다.
- `gcp_workloads_cli.build_gcp_workloads_activities`의 `aws_profile=` 한 줄만(48 파일).
- `infra/demo/compose.yaml`(45 파일): api `PLATFORM_ECR_AWS_PROFILE` 값, worker `PLATFORM_ECR_AWS_PROFILE`·`PROVISIONING_PLATFORM_EGRESS_CIDRS` 추가, worker `AWS_PROFILE` 삭제. `ensure-secrets.sh`(45 파일): pull 프로필 블록 하나.
- `infra/demo/build-stack.yaml`·`deploy-build-stack.sh`는 47만 고쳤다.

## 수용 기준
- [ ] 데모 계정에서 AWS 대상 Apply가 권한 오류 없이 진행된다. — 템플릿 v2가 Terraform·워크로드 호출 136건을 IAM 시뮬레이션으로 `allowed`, 금지 17건 거부. 실제 Apply는 승인 대기
- [ ] 다른 계정 EKS가 플랫폼 ECR 이미지를 받는다. — 저장소 정책 템플릿·변경 세트 완료. 실측은 다른 계정과 승인 필요
- [ ] 발급 토큰으로 push가 거부된다. — pull 역할 정책 시뮬레이션: `PutImage`·`InitiateLayerUpload`·`UploadLayerPart`·`CompleteLayerUpload` `implicitDeny`, 다른 저장소 pull 거부. 실제 `docker push` 403은 스택 반영 후

## Test Seam
- `tests/test_aws_target_access.py`: 템플릿을 렌더해 IAM 규칙(명시 거부 우선, 조건 키 없으면 실패)으로 평가. 호출 목록 전부 허용, 금지 목록 거부, 관대 해석에서도 부모 리소스 태그 필요, Terraform 리소스 타입·관리형 정책과 목록 일치, 접두어 고정·인라인 정책 없음, 정책 크기
- `tests/test_aws_cluster_access.py`: access_config·access entry·SG 규칙 태그(정적), 입력 검증 코드, tfvars·출력·인계 ref, Preview 표시·경고, egress 검사(포함·상위 CIDR·누락·형식), Plan이 명령 전에 거절, 미설정 경고, PlanPublisher·worker 설정 전달, 템플릿 v4
- `tests/test_platform_ecr_access.py`: 저장소 정책 조건, pull 역할 권한, 인스턴스 역할 Assume, 배포 스크립트(계정 목록 유지·검증·출력), `ensure-secrets.sh` pull 프로필, compose api·worker 프로필·전역 `AWS_PROFILE` 없음, GKE 토큰 프로필
- `tests/test_image_build.py`: Build의 `aws` 호출만 `AWS_PROFILE`을 받고 docker는 받지 않음, 어댑터 전달
- 갱신: `test_connection_templates.py`(v2 구조), `test_connection_launch_stack.py`(v2), `test_build_registry_runtime.py`·`test_demo_runtime.py`(새 역할·프로필), `test_aws_preview.py`·`test_aws_apply.py`·`test_plan_publish.py`(v4·새 경고)

## 승인 대기 (각 단계 사용자 승인 필요, 이 PR은 실행하지 않음)
main 병합 뒤 깨끗한 체크아웃에서:
1. `bash infra/demo/deploy-build-stack.sh --plan`으로 변경 세트를 다시 보고 `bash infra/demo/deploy-build-stack.sh` — `EcrPullRole` 생성, `BuildAccessPolicy`에 `AssumeEcrPullRole` 추가. 출력의 `PLATFORM_ECR_PULL_ROLE_ARN`·`PLATFORM_ECR_AWS_PROFILE` 두 줄을 SSM으로 `/opt/cmp/.env`에 추가. 다른 계정 대상이 생기면 `CMP_ECR_PULL_ACCOUNT_IDS=<계정> bash infra/demo/deploy-build-stack.sh`.
2. `bash infra/demo/deploy-connection-templates.sh --plan` → `bash infra/demo/deploy-connection-templates.sh` — 버킷 정책 `connections/v2/*`, 템플릿 v2 업로드. 출력된 `CONNECTION_TEMPLATE_BASE_URL`(…/connections/v2)로 `.env` 교체.
3. `/opt/cmp/.env`에 `PROVISIONING_PLATFORM_EGRESS_CIDRS=13.209.240.49/32`(데모 EIP, 실제 값 확인 후) 추가 → `bash infra/demo/release.sh HEAD`.
4. 서버 점검: README 「빌드 실행 환경 → 서버 점검」의 pull 프로필 두 줄. api에서 pull 토큰을 받아 `expiresAt`이 12시간인지(D17), 그 토큰으로 `docker push`가 403인지(테스트 태그, IMMUTABLE 저장소).
5. 테스트 범위·비용·정리 규칙에 따라: 플랫폼 계정(또는 테스트 계정)에 `target-role.yaml` v2 스택 → 연결 등록·verify → `cmp-` 클러스터 Plan→승인→Apply→워크로드→destroy(53). CloudTrail `AccessDenied`가 있으면 호출 목록·템플릿에 반영.

## 관련 문서
- [ADR-005 후속 메모](../../docs/decisions/ADR-005.md) · [46](46-connection-credentials.md) · [23b](23b-connection-launch-stack-hotfix.md) · [28](28-build-registry-runtime.md) · [29](29-image-pull-strategy.md) · [41](41-demo-ecr-pull-wiring.md) · [shared-contracts](../../specs/provisioning/shared-contracts.md) · [`infra/demo/README.md`](../../infra/demo/README.md) · [`infra/terraform/aws/README.md`](../../infra/terraform/aws/README.md)

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인 (실계정 실행 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 — ADR-005 후속 메모, shared-contracts, plan.md, 두 README. HTTP 계약·DB 변경 없음
- [ ] 구현·계약 정합성 및 최종 리뷰 — 조정 세션 리뷰 대기
- [x] 미검증 항목과 결과 증거 기록

## 변경 요약
- 템플릿: `infra/connections/target-role.yaml` v2(정책 둘), `infra/connections/simulate-target-role.py`(신규, 읽기 전용)
- Terraform: `main.tf`(access_config, SG 규칙 태그), `access.tf`(신규), `variables.tf`, `outputs.tf`
- Providers: `aws_target_access.py`·`platform_egress.py`(신규), `aws_preview.py`(입력·검증·Preview·egress·v4), `aws_apply.py`(출력 ref)
- Worker: `config.py`·`plan_publish.py`·`activities/plan_publish.py`(egress 설정), `image_build.py`·`build_wiring.py`(Build 프로필), `activities/gcp_workloads_cli.py`(GKE 토큰 프로필). API: `plan_inputs.py`(v4)
- 데모: `build-stack.yaml`, `deploy-build-stack.sh`, `deploy-connection-templates.sh`(v2), `ensure-secrets.sh`, `compose.yaml`, `README.md`
- 문서: 이 티켓, `tasks.md`, ADR-005, `shared-contracts.md`, `plan.md`, `infra/terraform/aws/README.md`

## 검증 기록 (2026-11-06, origin/main `93a18d7` 기준)
| 명령·환경 | 결과 |
| --- | --- |
| 새 테스트(구현 전) | `test_aws_target_access.py`: v1 템플릿에서 필요한 호출 15건 거부·금지 9건 허용 등 RED. `test_aws_cluster_access.py` 수집 실패(`platform_egress` 없음). `test_platform_ecr_access.py` 9건 실패 |
| `uv run python infra/connections/simulate-target-role.py --profile cmp-aws-prod`(IAM `simulate-custom-policy`, 읽기 전용) | v2: **136/136 일치**(허용 119·거부 17). 같은 질문으로 v1(origin/main): 112/136 — 필요한 호출 14건 거부(`CreateSubnet`·`CreateRouteTable`·`CreateSecurityGroup`·`CreateNatGateway`의 부모 리소스, `AuthorizeSecurityGroupEgress` 규칙 리소스, `DisassociateAddress` ENI, OIDC thumbprint 갱신, access entry 6건), 금지 10건 허용(사용자 VPC에 서브넷 생성·태그 부여, `AdministratorAccess` 연결, 인라인 정책, 플랫폼 태그 역할 생성·태깅, 사용자 역할 정책 조회, 사용자 클러스터 삭제·조회, 사용자 데이터 조회) |
| pull 역할 정책 `simulate-custom-policy` | 이 저장소 pull 3동작·`GetAuthorizationToken` `allowed`, push 4동작·`DeleteRepository`·다른 저장소 pull `implicitDeny` |
| `uvx cfn-lint` `target-role.yaml`·`source-role.yaml`·`build-stack.yaml` | 오류 없음 |
| `terraform validate`(docker `hashicorp/terraform:1.16.4`, AWS Provider 6.71.0, backend 제외 사본) · `terraform fmt -check` | `Success! The configuration is valid.` · 통과 |
| `make check`(ruff + pytest) | ruff 통과, 2349 passed, 9 skipped |
| `bash infra/demo/deploy-build-stack.sh --plan`(변경 세트만, 실행 안 함) | `cmp-demo-build`: `EcrPullRole`(AWS::IAM::Role) **Add**, `BuildAccessPolicy` **Modify**(PolicyDocument, 교체 없음). `BuildRepository` 변경 없음(`PullAccountIds` 비어 있음) |
| `bash infra/demo/deploy-connection-templates.sh --plan`(변경 세트만) | `cmp-demo-connection-templates`: `TemplateBucketPolicy` **Modify**(공개 접두어 `connections/v1/*` → `v2/*`, 교체 없음) |
| `make openapi` | 차이 없음(HTTP 변경 없음) |

### 미검증·제한 사항
- 대상 역할로 한 실제 Apply·destroy(B-14 최종 확인). 호출 목록은 Provider 6.71 동작에 대한 판단이고 SAR·시뮬레이터는 실제 요청 키를 재현하지 못한다. 사용자 계정의 SCP·권한 경계도 시뮬레이션 밖이다.
- 다른 계정 노드의 실제 pull, pull 토큰의 실제 수명(D17), 토큰으로 `docker push` 403.
- `iam:PassedToService`가 노드 그룹·애드온에서 `eks.amazonaws.com`인지(템플릿은 `ec2.amazonaws.com`도 허용해 어느 쪽이든 통과).
- 웹 입력 화면·44 카탈로그의 `cluster_admin_principal_arns`, 인계 화면 표시(협의 필요).
- 데모 서버 반영(SSM)은 하지 않았다.
