# 대회 데모 플랫폼 단일 EC2 배포 (#27)

이 구성은 `cmp-aws-prod`의 계정 `714066696240`, 서울 리전에 단일 EC2(`t3.large`, 암호화된 80 GiB EBS)와 S3 버킷 두 개를 만든다. 기본 VPC의 공개 Subnet을 사용하되 Security Group에는 인바운드 포트가 없다. SSM으로만 접속한다. S3 버킷은 Terraform State·잠금용과 커밋한 배포 소스 전달용으로 분리한다. **대상 EKS와 앱/DB 마이그레이션 리소스는 만들지 않는다.** EC2, EBS, 공개 IPv4, S3에 비용이 발생한다.

## 배포

검토된 커밋에서 실행한다. 스크립트는 계정 ID를 다시 확인하고 CloudFormation Stack을 생성/갱신한 뒤, 해당 커밋의 Git archive를 S3에 올려 SSM으로 배포한다. 로컬 AWS 자격 증명을 서버로 복사하지 않는다.

```sh
bash infra/demo/deploy.sh
```

서버에서는 Docker Compose로 FastAPI, 전체 Worker, Temporal, 업무 PostgreSQL, Temporal PostgreSQL, Temporal UI를 실행한다. DB 비밀번호와 Agent 운영자 키는 처음 기동할 때 `/opt/cmp/.env`에 생성하고 재배포 시 보존한다. DB 데이터는 루트 EBS의 `/opt/cmp/data/`에 있고, 서비스에는 `restart: unless-stopped`가 설정돼 있다. 이 배포는 대회용 단일 호스트이며 고가용성이나 운영용 Temporal 구성이 아니다.

## 접속과 확인

SSM Session Manager의 포트 포워딩으로 로컬 `8000`을 EC2 `8000`에 연결하면 `/health`와 `/docs`를 볼 수 있다. Temporal UI는 같은 방식으로 `8080`에 접속한다. API/Temporal UI는 EC2의 loopback에만 바인딩되며 공개 URL은 없다.

```sh
aws ssm start-session --profile cmp-aws-prod --region ap-northeast-2 \
  --target INSTANCE_ID \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
```

## 비용과 정리

대회 후 데이터와 State 보존 여부를 먼저 확인한다. Stack 삭제만으로 S3 버킷 및 EBS 볼륨은 삭제되지 않도록 보존했다. 소유자가 DB/Plan/State 백업을 확인한 뒤 별도 수동 삭제한다. `terraform destroy`나 자동 정리는 실행하지 않는다. 공개 API가 필요해지면 인증·TLS·접근 범위를 정한 뒤 별도 변경으로 추가한다.
