# 대회 데모 플랫폼 단일 EC2 배포 (#27)

이 구성은 `cmp-aws-prod`의 계정 `714066696240`, 서울 리전에 단일 EC2(`t3.large`, 암호화된 80 GiB EBS)와 S3 버킷 두 개를 만든다. 기본 VPC의 공개 Subnet을 사용한다. 운영 접속은 SSM으로만 하며, Security Group 인바운드는 Agent용 HTTPS 443뿐이다(#100, 템플릿 반영 후). S3 버킷은 Terraform State·잠금용과 커밋한 배포 소스 전달용으로 분리한다. **대상 EKS와 앱/DB 마이그레이션 리소스는 만들지 않는다.** EC2, EBS, 공개 IPv4, S3에 비용이 발생한다.

## 배포

검토된 커밋에서 실행한다. 스크립트는 계정 ID를 다시 확인하고 CloudFormation Stack을 생성/갱신한 뒤, 해당 커밋의 Git archive를 S3에 올려 SSM으로 배포한다. 로컬 AWS 자격 증명을 서버로 복사하지 않는다.

```sh
bash infra/demo/deploy.sh
```

최초 인프라 준비가 끝난 뒤에는 [GitHub Actions의 `Deploy demo`](../../.github/workflows/deploy-demo.yml)를 `main`에서 수동 실행한다. PR 검사는 기존 `Check` 워크플로가 담당한다. 이 배포 버튼은 GitHub OIDC로 `cmp-demo-github-deploy` 역할을 잠시 사용하며, 역할은 `main`의 이 저장소에만 열려 있다. 릴리스 단계는 EC2·S3·IAM 구성을 갱신하지 않는다. 로컬에서 같은 릴리스 단계만 실행할 때는 다음 명령을 사용한다.

```sh
bash infra/demo/release.sh HEAD
```

수동 워크플로의 `revision`에 이전 `main` 커밋 SHA를 넣으면 해당 소스를 재배포할 수 있다. 실행 중에는 동일 EC2의 다른 배포를 잠금으로 막는다. 새 릴리스의 API·Worker·DB 상태 확인이 실패하면 서버에 보관한 이전 릴리스로 복구를 시도하고 워크플로는 실패로 표시한다. DB 마이그레이션은 자동으로 되돌리지 않으므로 대회 기간에는 이전 코드와 호환되는 스키마 변경만 사용한다. API 컨테이너 교체 중 짧은 연결 중단은 발생할 수 있다. 실제 Terraform Apply가 Worker에 연결되면 실행 중인 Apply를 확인하고 안전하게 Worker를 교체하는 절차가 추가로 필요하다.

서버에서는 Docker Compose로 FastAPI, 전체 Worker, Temporal, 업무 PostgreSQL, Temporal PostgreSQL, Temporal UI를 실행한다. DB 비밀번호와 Agent 운영자 키는 처음 기동할 때 `/opt/cmp/.env`에 생성하고 재배포 시 보존한다. DB 데이터는 루트 EBS의 `/opt/cmp/data/`에 있고, Worker에는 향후 Plan 산출물용 `/opt/cmp/data/plans`를 마운트한다. 서비스에는 `restart: unless-stopped`가 설정돼 있다. 이 배포는 대회용 단일 호스트이며 고가용성이나 운영용 Temporal 구성이 아니다. 실제 Terraform Plan/Apply 및 승인 API 연결은 후속 구현이다.

## Provisioning 실행 환경 (#100, ticket 21)

worker 이미지는 `infra/demo/Dockerfile`의 `worker` 대상이며 Terraform 1.17.0, kubectl v1.37.1, Helm v4.3.1, AWS CLI 2.35.21, gcloud 588.0.0(+`gke-gcloud-auth-plugin`)을 버전·SHA-256으로 고정한다. API는 도구가 없는 `api` 대상을 쓴다.

두 대상 모두 런타임 리소스(`infra/terraform`, `infra/helm`, `catalog`, `skills`)를 `/app` 아래 저장소와 같은 상대 경로로 복사하고 `CMP_RESOURCE_ROOT=/app`을 지정한다(ticket 43). 패키지는 site-packages에 설치되므로 코드는 자기 위치가 아니라 이 값으로 템플릿·차트를 찾는다(`cmp_providers.resources`). 새 리소스 디렉터리를 코드에서 쓰면 `RUNTIME_RESOURCE_DIRS`와 Dockerfile `COPY`를 함께 고친다(`tests/test_worker_image_resources.py`가 대조). Terraform lock 파일은 `linux_amd64`·`linux_arm64`·`darwin_arm64`·`darwin_amd64` 해시를 담는다.

| 설정 | 서비스 | 값·출처 |
| --- | --- | --- |
| `DATABASE_URL`, `TEMPORAL_ADDRESS` | api, worker | 업무 PostgreSQL·Temporal (compose 내부망) |
| `ARTIFACT_STORE_DIR=/opt/cmp/artifact-store` | api, worker | 호스트 `/opt/cmp/data/artifact-store` 공유 볼륨 |
| `PROVISIONING_ARTIFACT_ROOT=/opt/cmp/plans` | api, worker | 호스트 `/opt/cmp/data/plans` 공유 볼륨 |
| `APPROVAL_ADMIN_KEY`, `AGENT_ENROLLMENT_ADMIN_KEY` | api만 | `/opt/cmp/.env`(0600), 없을 때만 생성 |
| `AWS_CONFIG_FILE=/run/cmp/aws-connections/config`, `PROVISIONING_AWS_PROFILE=cmp-demo-provisioning` | worker | api가 만든 연결 설정 파일(읽기 전용, 아래 「환경 연결」) = `/opt/cmp/secrets/aws/config`의 플랫폼 프로필 + 연결 프로필 `cmp-conn-*`(티켓 46). 플랫폼 프로필은 EC2 인스턴스 역할(`credential_source = Ec2InstanceMetadata`), `.env`의 `PROVISIONING_AWS_ROLE_ARN`이 있으면 그 역할을 Assume. 연결 없는 Plan과 State backend가 이 프로필을 쓴다 |
| `AWS_CONFIG_FILE=/run/cmp/aws/config`, `CONNECTION_PLATFORM_AWS_PROFILE` | api | `/opt/cmp/secrets/aws/config`(읽기 전용). 환경 연결(23)의 플랫폼 주체: 기본 `cmp-demo-build`(인스턴스 역할), 연결 주체 스택 배포 뒤 `.env`에서 `cmp-platform-connector`(티켓 46). api에는 GCP 키·gcloud 구성을 주지 않는다 |
| `PLATFORM_ECR_AWS_PROFILE=cmp-demo-build` | api | Agent ECR pull 토큰 발급(29) 주체. 연결 주체와 분리(연결 주체 역할에는 ECR 권한이 없다, 티켓 46) |
| `CONNECTION_AWS_CONFIG_FILE`, `CONNECTION_AWS_BASE_CONFIG_FILE` | api | 생성 파일 `/run/cmp/aws-connections/config`(호스트 `/opt/cmp/data/aws-connections`, api 쓰기·worker 읽기), 기본 파일 `/run/cmp/aws/config`. 키는 없다(ARN·External ID·프로필 이름). api가 시작·역할 등록·연결 삭제 때 DB에서 다시 쓴다 |
| `CONNECTION_PLATFORM_PRINCIPAL_ARN`, `CONNECTION_TEMPLATE_BASE_URL` | api | `/opt/cmp/.env`(`deploy-connector-stack.sh` 출력). 비면 `launch_stack_url`·`platform_principal_arn`이 `null` |
| `PLATFORM_ECR_REGISTRY`, `PLATFORM_ECR_REPOSITORIES` | api | `/opt/cmp/.env`. 레지스트리가 비면(기본) ON_PREM 작업에 플랫폼 레지스트리가 없고 토큰을 발급하지 않는다(29 이전 동작). 저장소 기본값 `cmp-platform-builds` |
| `CMP_GCP_PROJECT`, `PROVISIONING_GCLOUD_CONFIGURATION=cmp-demo` | worker | 운영자가 둔 `/opt/cmp/secrets/gcp/key.json`(읽기 전용) → 진입점이 tmpfs gcloud 구성·ADC 생성 |
| `CMP_PUBLIC_HOST` | edge | 비어 있으면 에지를 띄우지 않는다. 예: `<EIP>.sslip.io` |
| `WEB_AUTH_SECRET` | web | `/opt/cmp/.env`에 없을 때만 생성(웹 로그인 세션 서명용). 웹은 `AUTH_SECRET`으로 받는다 |
| `AUTH_GITHUB_ID`, `AUTH_GITHUB_SECRET`, `GEMINI_API_KEY` 등 | web | 운영자가 `/opt/cmp/web.env`(0600)에 직접 둔다. 저장소·릴리스 아카이브에 넣지 않는다 |

`.env`·`/opt/cmp/secrets/`는 서버에만 있고 저장소·이미지·릴리스 아카이브에 들어가지 않는다. 키 값을 터미널이나 로그에 출력하지 않는다.

### 공개 HTTPS 에지

`edge`(Caddy)는 443에서만 듣고 ACME TLS-ALPN-01로 인증서를 받아 `/opt/cmp/data/caddy`에 보관한다. 경로는 둘로 나뉜다.

- `/agents/*`, `/relays/*` → API(`api:8000`). Agent API와 중계 서버가 읽는 `GET /relays/{relay_id}/agents`다.
- 그 밖의 모든 경로 → 웹 화면(`web:3000`).

승인·운영자·터널 관리 API와 `/docs`, `/openapi.json`은 에지가 API로 넘기지 않으므로 계속 SSM 포트 포워딩으로만 쓴다(웹 서버 코드가 내부망에서 `http://api:8000`을 부를 뿐, 받은 경로를 그대로 전달하지 않는다). `/agent-enrollments`, `/docs` 같은 경로를 인터넷에서 열면 API가 아니라 웹이 404를 돌려준다. Traffic 중계 서버(별도 호스트, DNS 전환 P0 제외)와 포트·도메인을 공유하지 않는다.

웹 컨테이너(`web`)는 포트를 열지 않고(`expose: 3000`) 에지 뒤에서만 받는다. 웹은 **`AGENT_ENROLLMENT_ADMIN_KEY`를 받지 않는다**: 웹의 `/api/agents` 경로는 아직 사용자 로그인이 없어서, 공개 웹이 이 키를 가지면 누구나 Agent 등록을 만들 수 있다. 키 없이는 연결 관리 화면이 목업 데이터로 동작한다. 로그인 확인이 생기면 운영자가 `/opt/cmp/web.env`에 넣는다.

웹 화면은 이 저장소가 아니라 `cloud-migration-web`에서 온다. 노트북에서 `bash infra/demo/release-web.sh <웹 저장소 경로>`로 올리면 `releases/web-<sha>.tar.gz`로 올라가고(인스턴스 역할은 `releases/*`만 읽을 수 있다), 서버에서 `/opt/cmp/web/<sha>`로 풀려 `/opt/cmp/web/current`가 가리키며 `web` 서비스가 다시 만들어진다. 이후 플랫폼 릴리스(`start.sh`)도 `/opt/cmp/web/current/Dockerfile`이 있으면 웹을 함께 올린다.

### 원격 반영 순서 (각 단계 사용자 승인 필요)

1. CloudFormation 갱신: `bash infra/demo/deploy.sh` — SG 443 인그레스, IMDS hop limit 2, Elastic IP 추가. EIP 연결로 공인 IP가 바뀐다. 공개 IPv4 비용 발생.
2. Provisioning 권한: 인스턴스 역할 또는 별도 역할(`PROVISIONING_AWS_ROLE_ARN`)에 대상 EKS/VPC/IAM과 State 버킷 권한을 검토 후 부여.
3. GCP 키: 서비스 계정 키를 SSM 세션에서 `/opt/cmp/secrets/gcp/key.json`(root, 0600)으로 두고 `.env`에 `CMP_GCP_PROJECT=<project>`를 추가. 키를 S3 배포 버킷이나 저장소로 옮기지 않는다.
4. `.env`에 `CMP_PUBLIC_HOST=<EIP>.sslip.io`(Stack 출력 `AgentEdgeDefaultHost`) 추가 후 `bash infra/demo/release.sh HEAD`.
5. 웹 화면: `/opt/cmp/web.env`에 GitHub 로그인 앱 값(`AUTH_GITHUB_ID`, `AUTH_GITHUB_SECRET`)을 두고(콜백 주소 `https://<CMP_PUBLIC_HOST>/api/auth/callback/github`), 노트북에서 `bash infra/demo/release-web.sh <웹 저장소 경로>`. 고정 IP가 정해진 뒤에 한다(호스트 이름이 IP에서 나온다).

### 서버 점검·스모크

```sh
# SSM 세션에서
docker exec cmp-demo-worker-1 sh -c 'terraform version; helm version --short; kubectl version --client; aws --version; gcloud --version | head -1'
docker exec cmp-demo-worker-1 aws sts get-caller-identity --profile cmp-demo-provisioning --query Account --output text
docker exec cmp-demo-worker-1 gcloud config get-value project --configuration cmp-demo
# 노트북에서 (공개 경로만 열려 있는지)
curl -s -o /dev/null -w '%{http_code}\n' https://<CMP_PUBLIC_HOST>/agents/x        # 401/404 (API 응답)
curl -s -o /dev/null -w '%{http_code}\n' https://<CMP_PUBLIC_HOST>/relays/x/agents     # 401 (API: 중계 서버 자격증명 필요)
curl -s -o /dev/null -w '%{http_code}\n' https://<CMP_PUBLIC_HOST>/                  # 200 (웹 화면)
curl -s -o /dev/null -w '%{http_code}\n' https://<CMP_PUBLIC_HOST>/agent-enrollments  # 404 (웹이 답한다. API로는 안 간다)
curl -s -o /dev/null -w '%{http_code}\n' https://<CMP_PUBLIC_HOST>/docs              # 404 (웹이 답한다. API로는 안 간다)
```

Agent 등록: 포트 포워딩한 API로 운영자 키를 써서 등록 토큰을 발급한 뒤 15의 Chart에 `platform.url=https://<CMP_PUBLIC_HOST>`로 설치한다. 등록·작업 수신이 되면 18의 Plan 게시 → 승인 → ON_PREM 실행 스모크를 진행한다(18 완료 후).

## 빌드 실행 환경 (#152, ticket 28)

빌드 모듈(B-01)이 데모 서버에서 S3 소스를 받아 플랫폼 ECR에 push하도록 **별도 스택** `cmp-demo-build`(`build-stack.yaml`)를 둔다. 기존 `cmp-demo-control-plane` 스택은 갱신하지 않는다(미반영 EIP 때문에 공인 IP가 바뀐다).

| 항목 | 값 |
| --- | --- |
| ECR 저장소 | `cmp-platform-builds` (`714066696240.dkr.ecr.ap-northeast-2.amazonaws.com/cmp-platform-builds`), `IMMUTABLE`, 푸시 시 스캔, 태그 없는 이미지만 7일 뒤 만료, 스택 삭제 시 보존 |
| 인스턴스 역할 인라인 정책 | `cmp-demo-build`: `ecr:GetAuthorizationToken`, 그 저장소의 pull·push, 배포 버킷 `sources/`·`tests/`·`builds/`의 `GetObject`·`PutObject`, 버킷 `ListBucket`. `releases/`는 읽기 전용 그대로 |
| worker 설정 | `BUILD_ECR_REPOSITORY`·`BUILD_SOURCE_BUCKET`·`BUILD_LOG_BUCKET`·`BUILD_PLATFORM`(`/opt/cmp/.env`, 비면 사전 빌드 이미지), `BUILD_AWS_PROFILE`·`AWS_PROFILE`=`cmp-demo-build`(인스턴스 역할 프로필) |
| api 설정 (#176, 티켓 41·46) | `PLATFORM_ECR_REGISTRY`(`/opt/cmp/.env`, 비면 꺼짐)·`PLATFORM_ECR_REPOSITORIES`(기본 `cmp-platform-builds`), `PLATFORM_ECR_AWS_PROFILE=cmp-demo-build`, AWS 설정 파일 읽기 전용 마운트. K3s Agent의 `GET /agents/{agent_id}/jobs/{job_id}/image-pull` 토큰을 이 프로필(인스턴스 역할)로 발급한다 |
| worker 빌드 | 이미지의 Docker CLI 28.5.2·buildx v0.37.2 → 마운트한 호스트 `/var/run/docker.sock`의 데몬 |

**소켓 위험**: Docker 소켓을 가진 컨테이너는 호스트 root와 같은 권한이다. worker에만 주며, 빌드하는 앱 Dockerfile의 `RUN`도 호스트 데몬에서 실행된다. 대회용 단일 호스트에서만 허용한다. 운영급 대안은 rootless BuildKit을 별도 서비스로 두고 worker가 `BUILDKIT_HOST`(TLS)로만 접속하거나, 빌드를 CodeBuild·Kaniko 같은 격리 실행으로 옮기는 것이다.

### 반영 (각 단계 사용자 승인 필요)

```sh
bash infra/demo/deploy-build-stack.sh --plan   # 변경 세트만 만들고 실행하지 않음
bash infra/demo/deploy-build-stack.sh          # 스택 생성·갱신, PLATFORM_ECR_REGISTRY와 BUILD_* 네 줄 출력
# SSM 세션에서 출력된 다섯 줄을 /opt/cmp/.env에 추가한 뒤
bash infra/demo/release.sh HEAD
```

스택이 이미 있으면 두 번째 명령은 변경 없이 출력만 다시 한다(`--no-fail-on-empty-changeset`). 레지스트리 값은 저장소 URI의 호스트(`714066696240.dkr.ecr.ap-northeast-2.amazonaws.com`)다. api는 `.env`를 다시 읽어야 하므로 릴리스(또는 api 재생성)가 필요하다.

### 서버 점검

```sh
# SSM 세션에서
docker exec cmp-demo-api-1 python -c 'import boto3; print(boto3.Session(profile_name="cmp-demo-build").client("sts").get_caller_identity()["Arn"])'   # .../cmp-demo-control-plane-InstanceRole-.../i-...
docker exec cmp-demo-api-1 python -c 'from cmp_api.onprem_rollout import configured_platform_registry as r; print(r())'   # PlatformRegistry(registry='714066696240.dkr.ecr...', ...)
docker exec cmp-demo-worker-1 sh -c 'docker version --format "{{.Server.Version}}"; docker buildx version'
docker exec cmp-demo-worker-1 aws sts get-caller-identity --query Arn --output text   # .../cmp-demo-control-plane-InstanceRole-.../i-...
docker exec cmp-demo-worker-1 aws ecr describe-repositories --region ap-northeast-2 --repository-names cmp-platform-builds --query 'repositories[0].imageTagMutability' --output text  # IMMUTABLE
docker exec cmp-demo-worker-1 python -c 'from cmp_worker.config import settings as s; from cmp_worker.build_wiring import BuildSettings as B; print(B.from_values(s.build_source_bucket, s.build_ecr_repository, s.build_log_bucket, s.build_platform, s.build_aws_profile))'
```

## 환경 연결 (#181, ticket 46)

사용자 계정 연결 역할(ADR-005)을 Assume하는 플랫폼 주체와 사용자용 템플릿 호스팅을 **별도 스택** `cmp-demo-connector`(`connector-stack.yaml`)에 둔다. `cmp-demo-control-plane`은 갱신하지 않는다(28과 같은 이유).

| 항목 | 내용 |
| --- | --- |
| 연결 주체 역할 `cmp-platform-connector` | 이름 고정(사용자 신뢰 정책의 `PlatformPrincipalArn`, 인스턴스 역할이 다시 만들어져도 그대로). 신뢰: 인스턴스 역할. 권한: `sts:AssumeRole` on `arn:aws:iam::*:role/cmp-*`, 조건 `aws:ResourceTag/cmp:managed-by = cloud-migration-platform`(두 연결 템플릿이 붙이는 태그)·`sts:ExternalId` `cmp-*`. 세션 최대 1시간 |
| 인스턴스 역할 인라인 정책 `cmp-demo-connector` | 위 역할만 Assume |
| 템플릿 버킷 `cmp-demo-714066696240-apne2-connections` | `connections/*`만 공개 읽기(CloudFormation 빠른 생성이 S3 URL을 요구), ACL 차단, SSE-S3, 버전 관리, TLS 강제, 스택 삭제 시 보존. `connections/v1/`은 한 번 올리면 바꾸지 않는다(바꾸면 `v2`) |
| 프로필 연쇄 | `cmp-conn-<hex>`(`source_profile = cmp-platform-connector`) → `cmp-platform-connector`(`credential_source = Ec2InstanceMetadata`) → 인스턴스 역할. `ensure-secrets.sh`는 `.env`에 `CONNECTION_PLATFORM_PRINCIPAL_ARN`이 있을 때만 연결 주체 프로필을 쓴다 |
| Provisioning | Plan 입력 `credential_ref.id`가 연결 ID(`conn-<hex>`)면 provider·`aws`·kubectl은 `cmp-conn-<hex>`, State backend는 `PROVISIONING_AWS_PROFILE`. 아니면 지금처럼 `PROVISIONING_AWS_PROFILE` 하나 |

### 반영 (각 단계 사용자 승인 필요)

```sh
bash infra/demo/deploy-connector-stack.sh --plan   # 변경 세트만 만들고 실행하지 않음
bash infra/demo/deploy-connector-stack.sh          # 스택 생성, connections/v1 업로드, .env 세 줄 출력
# SSM 세션에서 출력된 세 줄을 /opt/cmp/.env에 추가한 뒤
bash infra/demo/release.sh HEAD
```

같은 키에 다른 내용의 템플릿이 이미 있으면 스크립트는 덮어쓰지 않고 실패한다(스크립트의 `template_version`을 올린다).

### 서버 점검

```sh
# SSM 세션에서
docker exec cmp-demo-api-1 python -c 'import boto3; print(boto3.Session(profile_name="cmp-platform-connector").client("sts").get_caller_identity()["Arn"])'   # ...:assumed-role/cmp-platform-connector/...
docker exec cmp-demo-worker-1 sh -c 'grep -c "^\[profile cmp-conn-" "$AWS_CONFIG_FILE"'   # 역할이 등록된 AWS 연결 수
```

## 접속과 확인

SSM Session Manager의 포트 포워딩으로 로컬 `8000`을 EC2 `8000`에 연결하면 `/health`와 `/docs`를 볼 수 있다. Temporal UI는 같은 방식으로 `8080`에 접속한다. API/Temporal UI는 EC2의 loopback에만 바인딩된다. 공개 URL은 Agent 경로(`/agents/*`)만 전달하는 에지뿐이다.

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

## 수동 CD 검증 (2026-11-04)

- 기존 EC2를 유지하며 CloudFormation `UPDATE_COMPLETE`: GitHub OIDC Provider와 `main`의 이 저장소만 신뢰하는 `cmp-demo-github-deploy` 역할 생성.
- `release.sh`로 커밋 `6e1af37ed548`의 릴리스 적용과 API·Worker·DB 상태 검사 통과.
- 의도적으로 실패하는 임시 릴리스에서 직전 `6e1af37ed548`로 복구되는 경로 확인. 임시 릴리스 디렉터리는 제거했다.
- GitHub Actions의 수동 버튼은 워크플로 파일이 `main`에 병합된 뒤 실행할 수 있으므로, GitHub OIDC에서 SSM까지의 전체 경로는 병합 후 한 번 검증해야 한다.

## 비용과 정리

대회 후 데이터와 State 보존 여부를 먼저 확인한다. Stack 삭제만으로 S3 버킷 및 EBS 볼륨은 삭제되지 않도록 보존했다. 소유자가 DB/Plan/State 백업을 확인한 뒤 별도 수동 삭제한다. `terraform destroy`나 자동 정리는 실행하지 않는다. 공개 API가 필요해지면 인증·TLS·접근 범위를 정한 뒤 별도 변경으로 추가한다.
