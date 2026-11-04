# 대회 데모 플랫폼 단일 EC2 배포 (#27)

이 구성은 `cmp-aws-prod`의 계정 `714066696240`, 서울 리전에 단일 EC2(`t3.large`, 암호화된 80 GiB EBS)와 S3 버킷 두 개를 만든다. 기본 VPC의 공개 Subnet을 사용하되 Security Group에는 인바운드 포트가 없다. SSM으로만 접속한다. S3 버킷은 Terraform State·잠금용과 커밋한 배포 소스 전달용으로 분리한다. **대상 EKS와 앱/DB 마이그레이션 리소스는 만들지 않는다.** EC2, EBS, 공개 IPv4, S3에 비용이 발생한다.

## 배포

검토된 커밋에서 실행한다. 스크립트는 계정 ID를 다시 확인하고 CloudFormation Stack을 생성/갱신한 뒤, 해당 커밋의 Git archive를 S3에 올려 SSM으로 배포한다. 로컬 AWS 자격 증명을 서버로 복사하지 않는다.

```sh
bash infra/demo/deploy.sh
```

최초 인프라 준비가 끝난 뒤에는 [GitHub Actions의 `Deploy demo`](../../.github/workflows/deploy-demo.yml)를 `main`에서 수동 실행한다. PR 검사는 기존 `Check` 워크플로가 담당한다. 이 배포 버튼은 GitHub OIDC로 `cmp-demo-github-deploy` 역할을 잠시 사용하며, 역할은 `main`의 이 저장소에만 열려 있다. 릴리스 단계는 EC2·S3·IAM 구성을 갱신하지 않는다. 로컬에서 같은 릴리스 단계만 실행할 때는 다음 명령을 사용한다.

```sh
bash infra/demo/release.sh HEAD
```

수동 워크플로의 `revision`에 이전 `main` 커밋 SHA를 넣으면 해당 소스를 재배포할 수 있다. 실행 중에는 동일 EC2의 다른 배포를 잠금으로 막는다. 새 릴리스의 API·Worker·DB 상태 확인이 실패하면 서버에 보관한 이전 릴리스로 복구를 시도하고 워크플로는 실패로 표시한다. DB 마이그레이션은 자동으로 되돌리지 않으므로 대회 기간에는 이전 코드와 호환되는 스키마 변경만 사용한다. API 컨테이너 교체 중 짧은 연결 중단은 발생할 수 있다.

서버에서는 Docker Compose로 FastAPI, 전체 Worker, Temporal, 업무 PostgreSQL, Temporal PostgreSQL, Temporal UI를 실행한다. DB 비밀번호와 Agent 운영자 키는 처음 기동할 때 `/opt/cmp/.env`에 생성하고 재배포 시 보존한다. DB 데이터는 루트 EBS의 `/opt/cmp/data/`에 있고, Worker에는 향후 Plan 산출물용 `/opt/cmp/data/plans`를 마운트한다. 서비스에는 `restart: unless-stopped`가 설정돼 있다. 이 배포는 대회용 단일 호스트이며 고가용성이나 운영용 Temporal 구성이 아니다. 실제 Terraform Plan/Apply 및 승인 API 연결은 후속 구현이다.

## 접속과 확인

SSM Session Manager의 포트 포워딩으로 로컬 `8000`을 EC2 `8000`에 연결하면 `/health`와 `/docs`를 볼 수 있다. Temporal UI는 같은 방식으로 `8080`에 접속한다. API/Temporal UI는 EC2의 loopback에만 바인딩되며 공개 URL은 없다.

```sh
aws ssm start-session --profile cmp-aws-prod --region ap-northeast-2 \
  --target i-091464203346381b5 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
```

## 실제 배포 확인 (2026-11-04)

- CloudFormation Stack: `cmp-demo-control-plane` (`CREATE_COMPLETE` 및 수정 후 `UPDATE_COMPLETE`).
- EC2: `i-091464203346381b5`, `t3.large`, 암호화된 80 GiB EBS `vol-0bb19790f4b29ef8c`. 기본 VPC에 있고 인바운드 보안 그룹 규칙은 없다.
- State: `cmp-demo-714066696240-apne2-state`; 버전 관리 `Enabled`, 기본 암호화 `AES256`, 네 가지 S3 공개 접근 차단 모두 활성화. EC2 역할에서 임시 `.tflock` 객체의 쓰기·읽기·목록·삭제를 확인했다.
- 배포 전달: `cmp-demo-714066696240-apne2-deploy`, 커밋별 `releases/` 아카이브.
- 컨테이너: 업무/Temporal PostgreSQL `healthy`, Temporal·Temporal UI·API·Worker `running`. API `/health`는 `{"status":"ok"}`, Temporal UI는 HTTP 200. 실제 Temporal Server에서 부수효과 없는 승인 대기·재개 smoke 테스트가 `scaffold_complete`로 완료됐다.
- 검증 범위: 플랫폼 스캐폴드 기동과 저장소 접근만 확인했다. AWS Terraform 최종 Plan, 승인 게이트, EKS Apply, 공개 웹 접속은 확인하거나 실행하지 않았다. 현재 AWS Plan 함수가 이름 붙은 로컬 프로필을 요구하므로 EC2 역할을 통한 Plan 실행은 후속 연결이 필요하다.

## 비용과 정리

대회 후 데이터와 State 보존 여부를 먼저 확인한다. Stack 삭제만으로 S3 버킷 및 EBS 볼륨은 삭제되지 않도록 보존했다. 소유자가 DB/Plan/State 백업을 확인한 뒤 별도 수동 삭제한다. `terraform destroy`나 자동 정리는 실행하지 않는다. 공개 API가 필요해지면 인증·TLS·접근 범위를 정한 뒤 별도 변경으로 추가한다.
