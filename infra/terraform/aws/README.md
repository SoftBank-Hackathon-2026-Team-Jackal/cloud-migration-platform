# AWS EKS 인프라 Plan (티켓 05, 이슈 #2)

이 Terraform 구성은 **신규** VPC, 최소 2개 가용 영역의 공개·비공개 Subnet, 인터넷 게이트웨이, NAT 게이트웨이 1개, Route, EKS 제어 영역 보안 그룹, IAM 역할, EKS 클러스터와 관리형 Node Group을 계획한다. 공개 Subnet에는 향후 인터넷 공개 ALB가 사용할 태그를 붙인다. ALB Controller·Ingress·TLS·EBS CSI·StorageClass·앱·DB 배포는 이 Plan에 포함되지 않는다. EKS API는 사설 Endpoint와 명시한 CIDR로 제한된 공개 Endpoint를 함께 사용한다. 비공개 Node는 NAT를 통해 ECR에 접근한다. 단일 NAT는 해당 가용 영역에 의존하며, **나중에 승인된 Apply를 실행할 때만** 비용이 발생한다.

입력은 `cmp_providers.aws_preview.AwsPlanInput`이다. 현재 `ProvisioningSpecV2`에는 대상·프로필·기능 참조만 있으므로 호출자가 구체적인 Network·Node 입력을 조회하고 검토해야 한다. v2에서 AWS 입력을 자동 생성한다고 주장하지 않는다. 실제 운영 승인을 위해서는 접근이 제한된 기존 S3 State 버킷과 고정 key `cmp/<environment_id>/terraform.tfstate`가 필요하다. Terraform 1.10 이상에서 `use_lockfile=true`와 암호화를 설정한다. 이 티켓은 버킷을 만들지 않는다. AWS Provider는 입력된 계정 ID와 리전으로 제한된다. 커밋된 `.terraform.lock.hcl`은 정적 검사에 사용한 Provider 버전을 고정한다. 실제 IAM 권한, AZ·EKS 버전·인스턴스 유형의 사용 가능 여부, 할당량, State 접근 권한, 기존 자원과의 충돌 여부는 운영 Plan에서 다시 확인해야 한다.

검토한 입력 JSON, AWS 계정, S3 Backend와 IAM 권한을 확인한 뒤 운영 Plan을 실행한다.

```sh
aws sts get-caller-identity --profile cmp-aws-prod
uv run python scripts/plan_aws_eks.py /protected/input.json /protected/plan-artifacts \
  --aws-profile cmp-aws-prod --terraform-bin terraform
```

**S3 버킷 없이 Plan만 검증**하려면 입력 JSON의 `state_bucket`을 `null`로 설정하고 `--local-validation`을 추가한다. 이 모드는 임시 복사본에서만 S3 Backend 블록을 제거하고 로컬 State를 사용한다. 원본 템플릿과 실제 실행한 템플릿의 Digest를 모두 기록한다. Preview에 `LOCAL_STATE_VALIDATION_ONLY` 미해결 결정을 넣어 공통 승인 계약이 해당 Plan을 거절하게 한다. 실제 클라우드 승인 전에는 기본 S3 모드로 **새로운** 최종 Plan과 Preview를 만들어야 한다. 로컬 검증 Plan을 Apply에 사용하지 않는다.

실행 함수는 Terraform 전에 AWS 계정을 다시 확인한다. 새 Plan ID·Revision 디렉터리에 `spec.json`, `saved.tfplan`, `manifest.json`, `preview.json`, `evidence.json`을 0700/0600 권한으로 저장한다. 같은 Revision을 덮어쓰지 않으며 `apply`를 호출하지 않는다. 바이너리 Plan과 입력 Spec은 내부 산출물이므로 접근을 제한하고 암호화해 보관하며 커밋하거나 출력하지 않는다. `evidence.json`에는 템플릿·Provider lockfile·Manifest Digest가, Manifest에는 정확한 Plan·Spec 바이트의 Digest와 대상 계정·State 식별·Plan ID·Revision·템플릿 버전이 담긴다. 영속적인 불변 산출물 저장소 연동과 승인 후 Apply는 후속 범위다.

Preview는 Terraform의 자원 종류·행위만 읽는다. `before`, `after`, `*_sensitive`, Output 값은 사용하지 않는다. 고정된 자원 이름과 검증한 Network·Node·API 접근 입력만 표시한다. 예상하지 못한 자원 종류나 행위는 Plan을 거절한다. `INFRASTRUCTURE` 범위는 앱·DB 배포까지 승인한다는 뜻이 아니다. ALB 미배포와 단일 NAT 제약도 경고로 표시한다.

`tests/fixtures/terraform/aws-create-plan.json`은 실제 AWS Provider Plan에서 자원 종류·행위만 남긴 fixture다. 민감정보 제거 검사를 위한 가짜 값만 포함하며 실제 Plan 바이트·State·Provider 출력값은 없다. 티켓 06은 GCP 자원에 맞는 별도 fixture가 필요하다. 공통 Plan/Preview 계약 fixture만으로 Provider별 Plan 검사를 대체하지 않는다.

AWS 자원을 변경하지 않는 정적 검사:

```sh
terraform fmt -check -recursive infra/terraform/aws
cd infra/terraform/aws
terraform init -backend=false -input=false -lockfile=readonly
terraform validate -no-color
```

## 승인된 Plan Apply (티켓 08)

`aws-eks-v2`부터 템플릿에 IAM OIDC Provider, EBS CSI 관리형 Add-on과 IRSA 역할, AWS Load Balancer Controller IRSA 역할·정책(공식 v3.6.2 `iam_policy.json`을 `controllers.tf`에 포함)과 비밀이 아닌 Output을 둔다. Controller Helm 설치, StorageClass, Ingress는 티켓 09에서 한다.

Apply는 07 승인 게이트를 통과한 Plan만 대상으로 한다. `claim_approved_execution` 이후 `execute_aws_plan` Activity(`cmp_providers.aws_apply.apply_aws_infrastructure`)가 다음을 처리한다.

- `PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/`의 `spec.json`·`saved.tfplan`을 manifest의 ArtifactRef(SHA-256·크기)와 대조한다.
- Spec의 계정·리전·State 식별·key(`cmp/<environment_id>/terraform.tfstate`)를 manifest와 비교한다.
- 로컬 검증 Plan(`state_bucket` 없음)은 거절한다.
- `evidence.json`의 템플릿·lockfile 지문을 현재 템플릿과 비교한다.
- STS 계정을 확인한다.

모두 통과하면 보호된 임시 작업 공간에서 S3 Backend로 `init`하고 **저장된 Plan만** `apply`한다. 재Plan·destroy·자동 재시도는 없다. 실패하면 State 자원 수만 기록한다. Plan이 stale이거나 실행이 중단되면 `UNKNOWN`으로 남기고 실제 상태 확인을 요구한다. 결과에는 비밀이 아닌 Output과 EKS 클러스터·Node Group 상태만 남기며 Terraform 출력 원문은 남기지 않는다.

Worker 설정은 `PROVISIONING_ARTIFACT_ROOT`, `PROVISIONING_AWS_PROFILE`(예: `cmp-aws-prod`), `TERRAFORM_BIN`이다. EKS는 Terraform 실행 주체에게 기본으로 클러스터 관리자 권한을 부여한다. 09는 같은 프로필로 클러스터에 접근한다.
