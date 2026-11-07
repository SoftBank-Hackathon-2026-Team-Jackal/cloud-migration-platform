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

worker 이미지는 `infra/demo/Dockerfile`의 `worker` 대상이며 Terraform 1.17.0, kubectl v1.37.1, Helm v4.3.1, AWS CLI 2.35.21, gcloud 588.0.0(+`gke-gcloud-auth-plugin`)을 버전·SHA-256으로 고정한다. 실제 분석이 소스를 가져오도록 배포판 `git`도 둔다(티켓 45). API는 도구가 없는 `api` 대상을 쓴다.

두 대상 모두 런타임 리소스(`infra/terraform`, `infra/helm`, `catalog`, `skills`)를 `/app` 아래 저장소와 같은 상대 경로로 복사하고 `CMP_RESOURCE_ROOT=/app`을 지정한다(ticket 43). 패키지는 site-packages에 설치되므로 코드는 자기 위치가 아니라 이 값으로 템플릿·차트를 찾는다(`cmp_providers.resources`). 새 리소스 디렉터리를 코드에서 쓰면 `RUNTIME_RESOURCE_DIRS`와 Dockerfile `COPY`를 함께 고친다(`tests/test_worker_image_resources.py`가 대조). Terraform lock 파일은 `linux_amd64`·`linux_arm64`·`darwin_arm64`·`darwin_amd64` 해시를 담는다.

| 설정 | 서비스 | 값·출처 |
| --- | --- | --- |
| `DATABASE_URL`, `TEMPORAL_ADDRESS` | api, worker | 업무 PostgreSQL·Temporal (compose 내부망) |
| `ARTIFACT_STORE_DIR=/opt/cmp/artifact-store` | api, worker | 호스트 `/opt/cmp/data/artifact-store` 공유 볼륨 |
| `PROVISIONING_ARTIFACT_ROOT=/opt/cmp/plans` | api, worker | 호스트 `/opt/cmp/data/plans` 공유 볼륨 |
| `APPROVAL_ADMIN_KEY`, `AGENT_ENROLLMENT_ADMIN_KEY` | api만 | `/opt/cmp/.env`(0600), 없을 때만 생성 |
| `AWS_CONFIG_FILE=/run/cmp/aws-connections/config`, `PROVISIONING_AWS_PROFILE=cmp-demo-provisioning` | worker | api가 만든 연결 설정 파일(읽기 전용, 아래 「환경 연결」) = `/opt/cmp/secrets/aws/config`의 플랫폼 프로필 + 연결 프로필 `cmp-conn-*`(티켓 46). 플랫폼 프로필은 EC2 인스턴스 역할(`credential_source = Ec2InstanceMetadata`), `.env`의 `PROVISIONING_AWS_ROLE_ARN`이 있으면 그 역할을 Assume. 연결 없는 Plan과 State backend가 이 프로필을 쓴다 |
| `AWS_CONFIG_FILE=/run/cmp/aws/config`, `CONNECTION_PLATFORM_AWS_PROFILE` | api | `/opt/cmp/secrets/aws/config`(읽기 전용). 환경 연결(23)의 플랫폼 주체 `cmp-demo-build`(인스턴스 역할, 23b·46). api에는 GCP 키·gcloud 구성을 주지 않는다. GCP 연결 확인은 worker Activity가 한다(48) |
| `PLATFORM_ECR_AWS_PROFILE`(기본 `cmp-demo-build`) | api·worker | Agent·GKE ECR pull 토큰 발급(29) 주체. 티켓 47부터 `/opt/cmp/.env`에 `PLATFORM_ECR_AWS_PROFILE=cmp-demo-ecr-pull`을 두면 pull 전용 역할로 발급한다(아래 「빌드 실행 환경」) |
| `PROVISIONING_PLATFORM_EGRESS_CIDRS` | worker | 티켓 47(D-10): worker가 새 클러스터 API에 나가는 주소(데모 EIP `/32`, 쉼표 구분). 설정하면 AWS Plan이 이 주소를 덮지 않는 `api_access_cidrs`를 `PLATFORM_EGRESS_NOT_ALLOWED`로 거절한다. 비면 Preview에 `PLATFORM_EGRESS_UNCHECKED` 경고. 값은 `/opt/cmp/.env`(예: `PROVISIONING_PLATFORM_EGRESS_CIDRS=13.209.240.49/32`) |
| `CONNECTION_AWS_CONFIG_FILE`, `CONNECTION_AWS_BASE_CONFIG_FILE` | api | 생성 파일 `/run/cmp/aws-connections/config`(호스트 `/opt/cmp/data/aws-connections`, api 쓰기·worker 읽기), 기본 파일 `/run/cmp/aws/config`. 키는 없다(ARN·External ID·프로필 이름). api가 시작·역할 등록·연결 삭제 때 DB에서 다시 쓴다 |
| `CONNECTION_PLATFORM_PRINCIPAL_ARN`, `CONNECTION_TEMPLATE_BASE_URL` | api | `/opt/cmp/.env`(`deploy-connection-templates.sh` 출력, 23b). 비면 `launch_stack_url`·`platform_principal_arn`이 `null` |
| `PLATFORM_ECR_REGISTRY`, `PLATFORM_ECR_REPOSITORIES` | api·worker | `/opt/cmp/.env`(두 컨테이너 같은 값). worker가 ON_PREM rollout 작업을 만들며 이미지 레지스트리를 검사하고, api가 pull 토큰을 발급한다. 레지스트리가 비면(기본) ON_PREM 작업에 플랫폼 레지스트리가 없고 토큰을 발급하지 않는다(29 이전 동작). 이때 ECR 앱 이미지는 `IMAGE_REGISTRY_NOT_ALLOWED`로 거부된다. 저장소 기본값 `cmp-platform-builds` |
| `ENV_FILE_STORE_DIR=/opt/cmp/env-files`, `ENV_FILE_ENCRYPTION_KEY` | api·worker(같은 디렉터리) | 업로드한 `.env` 암호화 저장소(호스트 `/opt/cmp/data/env-files`). 키는 `/opt/cmp/.env`에 없을 때만 생성(base64url 32바이트), 바꾸지 않는다(티켓 45) |
| 분석·Sandbox·Claude (`ANALYSIS_*`, `SANDBOX_*`, `LLM_ENABLED`, `ANTHROPIC_API_KEY`) | worker | `/opt/cmp/.env`, 비면 scaffold(아래 「분석·Sandbox·ENV 실행 설정」) |
| `CMP_GCP_PROJECT`, `PROVISIONING_GCLOUD_CONFIGURATION=cmp-demo` | worker | 운영자가 둔 `/opt/cmp/secrets/gcp/key.json`(읽기 전용, `cmp-provisioning@sbh26-prod` 키) → 진입점이 tmpfs gcloud 구성·ADC 생성. 같은 ADC로 GCP Plan·Apply와 `GCP_TARGET` 연결 확인(48)을 한다 |
| `CMP_PUBLIC_HOST` | edge | 비어 있으면 에지를 띄우지 않는다. 예: `<EIP>.sslip.io` |
| `WEB_AUTH_SECRET` | web | `/opt/cmp/.env`에 없을 때만 생성(웹 로그인 세션 서명용). 웹은 `AUTH_SECRET`으로 받는다 |
| `AUTH_GITHUB_ID`, `AUTH_GITHUB_SECRET`, `GEMINI_API_KEY` 등 | web | 운영자가 `/opt/cmp/web.env`(0600)에 직접 둔다. 저장소·릴리스 아카이브에 넣지 않는다 |

`.env`·`/opt/cmp/secrets/`는 서버에만 있고 저장소·이미지·릴리스 아카이브에 들어가지 않는다. 키 값을 터미널이나 로그에 출력하지 않는다.

### 공개 HTTPS 에지

`edge`(Caddy)는 443에서만 듣고 ACME TLS-ALPN-01로 인증서를 받아 `/opt/cmp/data/caddy`에 보관한다. 경로는 둘로 나뉜다.

| 경로 | 보내는 곳 | 쓰는 쪽 · 보호 |
| --- | --- | --- |
| `/agents/*` | API(`api:8000`) | 온프레미스 Agent. Agent 자격 증명 |
| `/relays/*` | API | 중계 서버가 읽는 `GET /relays/{relay_id}/agents`. 중계 서버 자격 증명 |
| `/migrations`, `/migrations/*` | API. `/migrations/*/events`(SSE)는 버퍼링 없이(`flush_interval -1`) | 브라우저(웹 실제 모드). **인증 없음**(#225: 대회 데모라 공개를 감수, 후속: 인증) |
| `/provisioning/*` | API | 브라우저의 Plan 미리보기·승인. 승인자 Bearer, 승인자 등록·Plan 입력은 `APPROVAL_ADMIN_KEY` |
| 그 밖의 모든 경로 | 웹 화면(`web:3000`) | `/`, `/settings`, `/api/*`(웹 서버 경로) 등 |

`/migrations`에서 누구나 이전을 시작할 수 있다(실제 시작은 대상 프로필·업로드한 `.env`가 있어야 하고, 없으면 422/503으로 거절된다). `/migrations/{id}/approve`(scaffold Signal)는 실제 경로의 Plan 승인을 대신하지 못하지만, `/migrations/{id}/cancel`·조회·로그는 Workflow ID(UUID)를 아는 사람이면 부를 수 있다. 데모가 끝나면 인증을 붙이거나 이 경로를 다시 닫는다. 이 무인증 공개는 데모 한정으로 감수한다는 결정이다(흐름 점검 N11 공백, [티켓 57](../../tickets/provisioning/57-demo-edge-sse-routes.md) E4). 운영 전환 전에는 사용자 인증(웹 로그인과 API 확인)이 먼저다.
- Temporal gRPC(`/temporal.api.*`, `/grpc.health.v1.Health/*`) → `temporal:7233`. **`CMP_TEMPORAL_ALLOWED_IP`(`/opt/cmp/.env`)에 적은 주소에서 온 요청만** 통과한다(중계 서버 `cmp-demo-relay`의 워커용). 설정하지 않으면 사용할 수 없는 문서용 주소(`192.0.2.1/32`)라 아무도 못 붙는다. 중계 서버 워커는 `TEMPORAL_ADDRESS=<CMP_PUBLIC_HOST>:443`, `TEMPORAL_TLS=true`로 접속한다.

운영자·Agent 등록·터널 관리 API(`/agent-admin/*`, `/agent-enrollments`, `/tunnel-admin/*`, `/provisioning-admin/*`, `/connections`, `/env-files`)와 `/docs`, `/openapi.json`은 에지가 API로 넘기지 않으므로 계속 SSM 포트 포워딩으로만 쓴다(웹 서버 코드가 내부망에서 `http://api:8000`을 부를 뿐, 받은 경로를 그대로 전달하지 않는다). `/agent-enrollments`, `/docs` 같은 경로를 인터넷에서 열면 API가 아니라 웹이 404를 돌려준다. Traffic 중계 서버(별도 호스트, DNS 전환 P0 제외)와 포트·도메인을 공유하지 않는다.

웹 컨테이너(`web`)는 포트를 열지 않고(`expose: 3000`) 에지 뒤에서만 받는다. 웹은 **`AGENT_ENROLLMENT_ADMIN_KEY`를 받지 않는다**: 웹의 `/api/agents` 경로는 아직 사용자 로그인이 없어서, 공개 웹이 이 키를 가지면 누구나 Agent 등록을 만들 수 있다. 키 없이는 연결 관리 화면이 목업 데이터로 동작한다. 로그인 확인이 생기면 운영자가 `/opt/cmp/web.env`에 넣는다.

웹은 **실제 모드로 빌드**된다(#225). `NEXT_PUBLIC_*`는 빌드 때 화면 코드에 들어가므로 compose `web.build.args`에 둔다: `NEXT_PUBLIC_API_MODE=sse`, `NEXT_PUBLIC_SSE_START=source`(고른 저장소가 있을 때만 `POST /migrations`), `NEXT_PUBLIC_API_BASE=https://<CMP_PUBLIC_HOST>`(같은 출처). `NEXT_PUBLIC_SSE_CAPABILITIES=${WEB_SSE_CAPABILITIES:-report,traffic.rollback,traffic.retry}`(Provisioning 77): 실제 모드에서 켤 화면 동작이다. 기본값은 플랫폼에 API가 있는 것만 넣는다(완료 리포트 `GET /migrations/{id}/report`, 트래픽 되돌리기·다시 시도 `POST /migrations/{id}/traffic/rollback|retry`). `sandbox.retry`는 플랫폼 API가 없어 뺀다. 바꾸려면 `/opt/cmp/.env`에 `WEB_SSE_CAPABILITIES=<쉼표 목록>`(`none` = 모두 끔)을 두고 웹을 다시 빌드한다. sse 빌드는 웹의 연결·Agent 화면 기본값도 실제 API로 바꾸므로, compose가 `PLATFORM_CONNECTIONS=${WEB_PLATFORM_CONNECTIONS:-mock}`으로 지금의 목업 화면을 유지한다. 실제 연결 화면을 쓰려면 `/opt/cmp/.env`에 `WEB_PLATFORM_CONNECTIONS=real`, `/opt/cmp/web.env`에 운영자 키를 둔다(위 주의 참고). 빌드 인자는 활성 플랫폼 릴리스의 compose에서 오므로 **플랫폼 배포(Deploy demo·`release.sh`)를 먼저 하고 `release-web.sh`로 웹을 다시 빌드**한다.

### 데모 승인자 토큰 (웹 `PLATFORM_APPROVER_TOKEN`)

승인 API(`POST /provisioning/plans/{plan_id}/revisions/{rev}/approvals`, `.../approvals/{id}/delivery`)와 Plan 미리보기·결과 조회는 승인자 Bearer가 없거나 틀리면 401 `APPROVER_AUTH_REQUIRED`다. 브라우저는 토큰을 갖지 않고, 웹 서버(BFF)가 `/opt/cmp/web.env`의 `PLATFORM_APPROVER_TOKEN`으로 대신 붙인다. 승인자는 데모용 1명(`demo-approver`)만 둔다.

- **Migration 권한**: 승인 권한은 승인자에게 등록된 `migration_id` 목록이다(티켓 07 D2, 판정 규칙은 그대로). 데모 이전은 시작할 때마다 새 ID(`migration-<uuid>`)를 받으므로, **이전을 시작한 뒤 승인 전에** 운영자가 그 ID를 같은 승인자에게 추가(`grant`)한다. 추가하지 않으면 그 이전의 미리보기·승인은 403 `APPROVER_FORBIDDEN`이다. 토큰은 바뀌지 않으므로 웹은 다시 만들 필요가 없다.
- **주의**: 웹이 이 토큰을 가지면, 웹에서 승인 버튼을 누를 수 있는 사람은 누구나 이 승인자로 승인한다. 대회 데모 동안만 두고 끝나면 폐기한다. 실제 사용자 인증은 후속이다.
- 스크립트 `infra/demo/approver-token.py`(표준 라이브러리만)는 서버 내부 API(`127.0.0.1:8000`)를 부른다. 운영자 키는 환경변수 `APPROVAL_ADMIN_KEY` 또는 숨김 프롬프트로만 받는다(인자 금지). 발급한 토큰은 한 번만 쓴다: `--env-file`이면 그 파일(0600)에 `PLATFORM_APPROVER_TOKEN=` 줄을 바꿔 넣고 화면에 찍지 않으며, 없으면 stdout에 한 줄만 낸다. DB에는 SHA-256 digest만 있어서 잃어버린 토큰은 다시 볼 수 없다(교체한다).

```bash
# SSM 세션에서 (root). 키 값은 히스토리·프로세스 인자에 남지 않는다(셸 내장 export로 환경변수에만 둔다).
sudo -i
cd /opt/cmp/releases/$(cat /opt/cmp/current-revision)
compose=(docker compose --env-file /opt/cmp/.env -f infra/demo/compose.yaml --profile public)
export "$(grep '^APPROVAL_ADMIN_KEY=' /opt/cmp/.env)"

# 1. 생성 + 웹 env에 넣기 (한 번만. 이미 있으면 409 APPROVER_EXISTS → 3번 교체를 쓴다)
#    --migration은 필수다. 아직 이전이 없으면 리허설 이전의 ID나 자리표시(demo-placeholder)를 넣는다.
python3 infra/demo/approver-token.py create demo-approver --migration demo-placeholder --env-file /opt/cmp/web.env
"${compose[@]}" up -d --force-recreate web    # 웹이 web.env를 다시 읽는다(이미지 재빌드 불필요)

# 2. 이전마다 권한 추가(grant). /opt/cmp/.env에 DEMO_APPROVER_ID=demo-approver를 두면(api 재생성) 시작 때
#    자동으로 추가된다(티켓 60 A12). 비어 있거나 로그에 DEMO_APPROVER_GRANT_FAILED가 있으면 아래처럼 손으로 한다:
#    웹에서 시작한 뒤 Plan이 게시되면 가장 최근 Migration ID를 추가한다.
mid="$("${compose[@]}" exec -T platform-db psql -U platform -d platform -Atc \
  "select migration_id from provisioning_plan_revisions order by published_at desc limit 1")"
python3 infra/demo/approver-token.py grant demo-approver --migration "$mid"

# 3. 교체(rotate): 새 토큰 발급, 이전 토큰은 즉시 401. 웹을 다시 만들기 전까지는 웹 승인도 401이다.
python3 infra/demo/approver-token.py rotate demo-approver --env-file /opt/cmp/web.env
"${compose[@]}" up -d --force-recreate web

# 4. 폐기(revoke): 대체 토큰 없이 무효화. 승인자 행·권한·지난 승인 기록은 남는다(다시 쓰려면 3번).
python3 infra/demo/approver-token.py revoke demo-approver
sed -i '/^PLATFORM_APPROVER_TOKEN=/d' /opt/cmp/web.env && "${compose[@]}" up -d --force-recreate web

# 확인: 토큰 없이는 401 (토큰 값은 찍지 않는다)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/provisioning/plans/p/revisions/1/approvals/a/delivery   # 401
unset APPROVAL_ADMIN_KEY
```

같은 일을 HTTP로 하면(모두 `X-Approval-Admin-Key` 필요, 틀리면 403): 생성 `POST /provisioning/approvers` `{approver_id, migration_ids}` → 201 `{approver_id, credential}`, 권한 추가 `POST /provisioning/approvers/{approver_id}/migrations` `{migration_ids}` → 200 `{approver_id, migration_ids}`, 교체 `POST /provisioning/approvers/{approver_id}/credential` → 200 `{approver_id, credential}`, 폐기 `DELETE /provisioning/approvers/{approver_id}/credential` → 204. 없는 승인자는 404 `APPROVER_NOT_FOUND`. 이 경로는 `/provisioning/*`라 에지로도 열려 있지만 운영자 키 없이는 403이므로, 키는 위처럼 SSM 세션 안에서만 쓴다.

웹 화면은 이 저장소가 아니라 `cloud-migration-web`에서 온다. 노트북에서 `bash infra/demo/release-web.sh <웹 저장소 경로>`로 올리면 `releases/web-<sha>.tar.gz`로 올라가고(인스턴스 역할은 `releases/*`만 읽을 수 있다), 서버에서 `/opt/cmp/web/<sha>`로 풀려 `/opt/cmp/web/current`가 가리키며 `web` 서비스가 다시 만들어진다. 이후 플랫폼 릴리스(`start.sh`)도 `/opt/cmp/web/current/Dockerfile`이 있으면 웹을 함께 올린다.

### 원격 반영 순서 (각 단계 사용자 승인 필요)

1. CloudFormation 갱신: `bash infra/demo/deploy.sh` — SG 443 인그레스, IMDS hop limit 2, Elastic IP 추가. EIP 연결로 공인 IP가 바뀐다. 공개 IPv4 비용 발생.
2. Provisioning 권한(티켓 47): 대상 EKS/VPC/IAM 권한은 인스턴스 역할에 주지 않는다. 같은 계정도 다른 계정처럼 `infra/connections/target-role.yaml`(v2) 스택을 만들어 대상 연결로 등록하고, Plan 입력 `credential_ref`를 그 연결로 둔다(46). 플랫폼 계정은 SCP로 서울 리전만 허용된다(점검 B-15, `aws:RequestedRegion` 시뮬레이션 기준) — 같은 계정 대상은 `ap-northeast-2`만 쓴다.
3. GCP 키: 먼저 `infra/demo/gcp-platform-identity.sh --configuration cmp-gcp-prod`(dry-run)로 확인하고 승인 뒤 `--apply`로 플랫폼 SA `cmp-provisioning`·노드 SA `cmp-gke-nodes`와 역할을 만든다(48). 그 SA의 키를 SSM 세션에서 `/opt/cmp/secrets/gcp/key.json`(root, 0600)으로 두고 `.env`에 `CMP_GCP_PROJECT=sbh26-prod`를 추가. 키를 S3 배포 버킷이나 저장소로 옮기지 않는다. 노드 풀 입력 `node_service_account_email`은 `cmp-gke-nodes@sbh26-prod.iam.gserviceaccount.com`.
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
curl -sI https://<CMP_PUBLIC_HOST>/migrations/does-not-exist                    # 405, allow: GET, content-type: application/json (API 응답, #225. 웹이면 text/html)
curl -s -o /dev/null -w '%{http_code}\n' https://<CMP_PUBLIC_HOST>/provisioning/plans/p/revisions/1/preview  # 401 APPROVER_AUTH_REQUIRED (API, Bearer 보호 유지)
curl -sN -m 5 https://<CMP_PUBLIC_HOST>/migrations/<workflow_id>/events          # 이벤트가 끊김 없이 바로 흘러나온다(SSE)
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
| worker 설정 | `BUILD_ECR_REPOSITORY`·`BUILD_SOURCE_BUCKET`·`BUILD_LOG_BUCKET`·`BUILD_PLATFORM`(`/opt/cmp/.env`, 비면 사전 빌드 이미지. 티켓 58: 기본 안내 `linux/amd64,linux/arm64`, 첫 항목이 샌드박스(이 호스트) 아키텍처), `BUILD_AWS_PROFILE=cmp-demo-build`(인스턴스 역할 프로필). 티켓 47(B-20): Build 모듈이 자기 `aws` 호출에 이 프로필을 직접 넘기고, worker 전역 `AWS_PROFILE`은 없다 |
| ECR pull 전용 역할 (티켓 47, B-09) | `cmp-demo-ecr-pull`(이 스택): `ecr:GetAuthorizationToken` + 이 저장소의 pull 세 동작만. 인스턴스 역할만 Assume. `ensure-secrets.sh`가 `.env`의 `PLATFORM_ECR_PULL_ROLE_ARN`이 있으면 프로필 `cmp-demo-ecr-pull`(`credential_source = Ec2InstanceMetadata`, 인스턴스 역할이 바로 Assume)을 쓴다. K3s·GKE pull Secret을 읽은 사람은 이 저장소 pull만 가능하고 push는 거부된다 |
| 교차 계정 pull (티켓 47, B-08) | 매개변수 `PullAccountIds`(기본 빈 값 = 저장소 정책 없음)에 대상 연결 계정을 넣으면 저장소 정책이 그 계정의 `arn:aws:iam::*:role/cmp-*-eks-node`(Terraform 노드 역할)에게 pull 세 동작만 허용한다. `CMP_ECR_PULL_ACCOUNT_IDS=<id>[,<id>] bash infra/demo/deploy-build-stack.sh --plan`으로 바꾸고, 비우면 이전 값을 유지한다. 같은 계정은 필요 없다 |
| api 설정 (#176, 티켓 41·46) | `PLATFORM_ECR_REGISTRY`(`/opt/cmp/.env`, 비면 꺼짐)·`PLATFORM_ECR_REPOSITORIES`(기본 `cmp-platform-builds`), `PLATFORM_ECR_AWS_PROFILE=cmp-demo-build`, AWS 설정 파일 읽기 전용 마운트. K3s Agent의 `GET /agents/{agent_id}/jobs/{job_id}/image-pull` 토큰을 이 프로필(인스턴스 역할)로 발급한다 |
| worker 빌드 | 이미지의 Docker CLI 28.5.2·buildx v0.37.2 → 마운트한 호스트 `/var/run/docker.sock`의 데몬 |

**소켓 위험**: Docker 소켓을 가진 컨테이너는 호스트 root와 같은 권한이다. worker에만 주며, 빌드하는 앱 Dockerfile의 `RUN`도 호스트 데몬에서 실행된다. 대회용 단일 호스트에서만 허용한다. 운영급 대안은 rootless BuildKit을 별도 서비스로 두고 worker가 `BUILDKIT_HOST`(TLS)로만 접속하거나, 빌드를 CodeBuild·Kaniko 같은 격리 실행으로 옮기는 것이다. rootless BuildKit도 `RUN`이 호스트를 거쳐 나가므로 아래 IMDS 차단은 따로 필요하다.

**빌드의 인스턴스 메타데이터 차단 (#189, 티켓 54)**: 내장 BuildKit의 `RUN`은 기본 bridge `docker0`에서 돌고, IMDS hop limit 2(티켓 21) 때문에 사용자 Dockerfile이 `169.254.169.254`에서 인스턴스 역할 자격을 꺼낼 수 있다(로컬 가짜 IMDS로 재현). `start.sh`가 worker를 올리기 전에 `sh infra/demo/build-imds-guard.sh install`을 실행한다. 이 스크립트는 `DOCKER-USER` 맨 앞에 `-i docker0 -d 169.254.169.254/32 -j DROP`을 넣고, `cmp-build-imds-guard.service`가 Docker가 시작될 때마다(재부팅 포함) 다시 넣는다. 규칙이 없으면 릴리스가 실패한다. compose 서비스(worker·api)는 자기 bridge(`br-*`)에 있어 인스턴스 역할을 그대로 쓴다. 빌드 모듈은 `--network default`를 고정하고 entitlement를 주지 않아 `RUN --network=host`(규칙 우회)는 거부된다. VPC DNS(`169.254.169.253`) 등 다른 주소와 인터넷 통신은 막지 않는다. IPv6 IMDS는 템플릿에서 꺼 둔다(`HttpProtocolIpv6: disabled`).

**멀티 아키텍처 빌드 (#278, 티켓 58)**: `BUILD_PLATFORM=linux/amd64,linux/arm64`이면 Build 모듈이 플랫폼마다 같은 내장 builder(`docker0`, 위 IMDS 차단 그대로)로 빌드하고 `docker buildx imagetools create`로 매니페스트 목록을 push한다. Plan의 노드 조건은 두 아키텍처를 모두 허용한다(Apple Silicon k3d 같은 arm64 대상). x86_64 호스트에서 arm64를 빌드하려면 QEMU binfmt가 필요하다. `start.sh`가 `sh infra/demo/build-binfmt.sh install`로 `tonistiigi/binfmt`(digest 고정, 네트워크 없음)를 실행하고, `cmp-build-binfmt.service`가 Docker가 시작될 때마다(재부팅 포함) 다시 등록한다. `.env`의 `BUILD_PLATFORM`에 arm64가 있으면 설치 실패가 릴리스 실패다. 에뮬레이션 빌드는 느리다(티켓 58 미검증 항목).

### 반영 (각 단계 사용자 승인 필요)

```sh
bash infra/demo/deploy-build-stack.sh --plan   # 변경 세트만 만들고 실행하지 않음
bash infra/demo/deploy-build-stack.sh          # 스택 생성·갱신, PLATFORM_ECR_REGISTRY·BUILD_* 네 줄·PLATFORM_ECR_PULL_ROLE_ARN·PLATFORM_ECR_AWS_PROFILE 출력
# SSM 세션에서 출력된 일곱 줄을 /opt/cmp/.env에 추가한 뒤(start.sh가 ensure-secrets.sh로 pull 프로필을 쓴다)
bash infra/demo/release.sh HEAD
```

스택이 이미 있으면 두 번째 명령은 변경 없이 출력만 다시 한다(`--no-fail-on-empty-changeset`). 레지스트리 값은 저장소 URI의 호스트(`714066696240.dkr.ecr.ap-northeast-2.amazonaws.com`)다. api와 worker는 `.env`를 다시 읽어야 하므로 릴리스(또는 api·worker 재생성)가 필요하다.

### 서버 점검

```sh
# SSM 세션에서
docker exec cmp-demo-api-1 python -c 'import boto3; print(boto3.Session(profile_name="cmp-demo-build").client("sts").get_caller_identity()["Arn"])'   # .../cmp-demo-control-plane-InstanceRole-.../i-...
docker exec cmp-demo-api-1 python -c 'from cmp_api.onprem_rollout import configured_platform_registry as r; print(r())'   # PlatformRegistry(registry='714066696240.dkr.ecr...', ...)
docker exec cmp-demo-worker-1 python -c 'from cmp_api.onprem_rollout import configured_platform_registry as r; print(r() is not None)'   # True (worker가 rollout 작업을 만든다)
docker exec cmp-demo-worker-1 sh -c 'docker version --format "{{.Server.Version}}"; docker buildx version'
iptables -S DOCKER-USER | grep cmp-build-imds-guard; systemctl is-enabled cmp-build-imds-guard.service   # 규칙 1줄, enabled (티켓 54)
systemctl is-enabled cmp-build-binfmt.service; docker exec cmp-demo-worker-1 docker buildx inspect default | grep Platforms   # enabled, linux/arm64 포함 (티켓 58)
docker buildx build --network default --no-cache --progress plain /opt/cmp/releases/<SHA>/infra/demo/imds-probe 2>&1 | grep 'imds_probe token'   # token_put=000 role_list=000, 빌드 성공
docker exec cmp-demo-worker-1 aws sts get-caller-identity --profile cmp-demo-build --query Arn --output text   # .../cmp-demo-control-plane-InstanceRole-.../i-...
# 티켓 47: pull 전용 토큰(api·worker 모두). worker는 공유 연결 파일을 읽으므로 api가 다시 시작된 뒤에 보인다(B-23)
docker exec cmp-demo-api-1 python -c 'import boto3; print(boto3.Session(profile_name="cmp-demo-ecr-pull").client("sts").get_caller_identity()["Arn"])'   # .../assumed-role/cmp-demo-ecr-pull/...
docker exec cmp-demo-worker-1 aws sts get-caller-identity --profile cmp-demo-ecr-pull --query Arn --output text   # 같은 역할
docker exec cmp-demo-worker-1 aws ecr describe-repositories --region ap-northeast-2 --repository-names cmp-platform-builds --query 'repositories[0].imageTagMutability' --output text  # IMMUTABLE
docker exec cmp-demo-worker-1 python -c 'from cmp_worker.config import settings as s; from cmp_worker.build_wiring import BuildSettings as B; print(B.from_values(s.build_source_bucket, s.build_ecr_repository, s.build_log_bucket, s.build_platform, s.build_aws_profile))'
```

## 데이터 이전 원본 쓰기 역할 (데이터 티켓 12)

데이터 이전은 원본 EKS `cmp-demo-source`에 쓰기가 필요하다. 원본 연결 역할(`AmazonEKSViewPolicy`)은 "연결은 읽기 전용" 원칙으로 그대로 두고, **별도 스택** `cmp-demo-data-migration`(`data-migration-stack.yaml`)으로 데이터 전용 역할을 둔다. 자세한 근거와 확인 결과는 `tickets/data-migration/12-source-write-role.md`.

| 항목 | 값 |
| --- | --- |
| IAM 역할 | `cmp-demo-data-migration`: 인스턴스 역할만 신뢰(ExternalId 없음), 인라인 `eks:DescribeCluster`(이 클러스터만) |
| EKS 접근 항목 | `AmazonEKSEditPolicy`, namespace 범위 `chat-source`·`cmp-data-tunnel`, 그룹 `cmp-data-migration` |
| Kubernetes 객체 | `data-migration-rbac.yaml`: Namespace `cmp-data-tunnel`, 그 Namespace 객체 하나의 get·patch(터널이 매번 server-side apply) |
| 인스턴스 역할 인라인 정책 | `cmp-demo-data-migration`: 이 역할 `sts:AssumeRole`만 |
| 프로필(이름 고정) | `cmp-demo-data-migration`. `.env`에 `DATA_MIGRATION_ROLE_ARN`이 있으면 `ensure-secrets.sh`가 `role_arn` + `credential_source = Ec2InstanceMetadata`로 쓴다(`source_profile` 없음, #265). worker는 `DATA_MIGRATION_AWS_PROFILE=cmp-demo-data-migration` |

```sh
bash infra/demo/deploy-data-migration-stack.sh --plan   # 변경 세트만
bash infra/demo/deploy-data-migration-stack.sh          # 스택 + RBAC(임시 kubeconfig), DATA_MIGRATION_ROLE_ARN 줄 출력
# 서버(SSM): /opt/cmp/.env에 출력 줄 추가 → release.sh. 확인:
docker exec cmp-demo-worker-1 aws sts get-caller-identity --profile cmp-demo-data-migration --query Arn --output text   # .../assumed-role/cmp-demo-data-migration/...
```

## 온프레미스 k3d API 경로: 중계 서버 TCP 전달 (티켓 74, ADR-007 4단계)

데모 서버 worker가 맥 k3d API에 닿는 경로다. 트래픽 영역(중계 서버)을 Provisioning이 대행했다(이상헌님 공유 필요). 사용자 HTTP 경로 Envoy(8080·9901)는 바꾸지 않고, 중계 서버에 **전달 전용 Envoy**(`envoy-kube`, 관리 127.0.0.1:9902)를 하나 더 둔다. 같은 호스트 네트워크에서 사용자 Envoy와 hot-restart 도메인 소켓이 겹치지 않게 `--base-id 1`로 띄운다(없으면 `errno=98`로 재시작 반복).

```text
worker(172.31.15.223) → 중계 서버 사설 IP:16400+터널 호스트 번호 → Agent 터널 IP:kube_api_port(6443) → kubernetes.default.svc:443
                        (예: 172.31.2.84:16402 → 10.200.0.2:6443, 출발지 10.200.0.1)
```

- kubeconfig(티켓 75): `server: https://<중계 서버 사설 IP>:<16400 + 터널 IP 마지막 옥텟>`, `tls-server-name: kubernetes.default.svc`. 규칙은 [티켓 74](../../tickets/provisioning/74-relay-kube-api-forward.md).
- 전제: 플랫폼 `TUNNEL_EXPOSE_KUBE_API=true`(`/opt/cmp/.env`, compose가 api에 넘긴다. 포트는 `TUNNEL_KUBE_API_PORT`, 기본 6443 — 78에서 연결), Agent Chart `tunnel.exposeKubeApi=true`(티켓 73).
- 중계 서버의 `python3`는 3.9라 pydantic이 없다. `enable-kube-api-forward.sh`는 peer-sync와 같은 `python3.11`을 쓴다(`PYTHON=`으로 바꿈, 78).

### 반영 (각 단계 사용자 승인 필요)

```sh
KUBE_API_FORWARD_SOURCE_SG=<데모 서버 SG> bash infra/relay/deploy-relay-stack.sh --plan   # TCP 16402-16654, 데모 서버 SG만
KUBE_API_FORWARD_SOURCE_SG=<데모 서버 SG> bash infra/relay/deploy-relay-stack.sh
# 중계 서버(SSM): /opt/cmp/lib 갱신 뒤
sudo infra/relay/enable-kube-api-forward.sh            # 사설 IP는 IMDSv2, 또는 BIND_IP=172.31.2.84
curl -s 127.0.0.1:9902/listeners                         # kube_api_16402::172.31.2.84:16402
```

## 온프레미스 데이터 터널 고정 원본 NLB (티켓 82, ADR-007 2안)

`ingress="source"` 데이터 터널은 원본 EKS에 공개 UDP NLB가 있어야 한다. 실행마다 새로 만들면 provisioning·헬스체크·DNS로 3~4분이 걸린다. `WEB_START_DATA_MIGRATION`의 `tunnel`에 `"standing_ingress": true`를 넣으면 실행은 `cmp-data-tunnel/cmp-wg-standing-endpoint` Service 하나를 재사용한다(없으면 그 실행이 만든다). 실행마다 selector(이번 원본 게이트웨이 Pod)와 `loadBalancerSourceRanges`(`target_egress_cidrs`)만 맞추고, 정리 때 이 Service는 남긴다. 게이트웨이·키 Secret·NetworkPolicy는 실행마다 새로 만들고 지운다. 한 번에 한 실행만 쓴다(데이터 영역 대행, 데이터 담당 공유 필요). 헬스체크는 5초 간격·임계 2로 줄였다.

### 시연 전 (각 단계 사용자 승인 필요)

```sh
aws sts get-caller-identity --profile cmp-aws-prod                       # 714066696240
curl -s https://checkip.amazonaws.com                                    # 맥 공인 IP
aws eks update-kubeconfig --name cmp-demo-source --region ap-northeast-2 --profile cmp-aws-prod --alias cmp-demo-source
make data-tunnel-nlb-up CONTEXT=cmp-demo-source CIDR=<맥 공인 IP>/32     # 주소 → NLB active → DNS 풀림까지 대기
make data-tunnel-nlb-status CONTEXT=cmp-demo-source                       # 고른 게이트웨이: 없음(대기)
# /opt/cmp/.env WEB_START_DATA_MIGRATION의 tunnel: {"ingress":"source","target_egress_cidrs":["<맥 공인 IP>/32"],"standing_ingress":true}
```

시연 뒤 `make data-tunnel-nlb-down CONTEXT=cmp-demo-source`로 지운다(NLB 시간당 비용). 실행이 게이트웨이를 고르고 있으면 `up`·`down`은 거절한다(`--force`로 덮어씀). 맥 공인 IP가 바뀌면 `up`을 다시 하거나 실행 입력의 `target_egress_cidrs`만 바꾼다(실행이 맞춘다).

## 환경 연결 Launch Stack (#205, 티켓 23b)

AWS 연결 생성 응답의 `launch_stack_url`(ADR-005)은 api가 `CONNECTION_PLATFORM_PRINCIPAL_ARN`과 `CONNECTION_TEMPLATE_BASE_URL`을 모두 받아야 만들어진다. 둘 중 하나라도 비면 `null`이다(이전 동작).

| 항목 | 값 |
| --- | --- |
| 템플릿 버킷 스택 | `cmp-demo-connection-templates`(`connection-templates-stack.yaml`). 버킷 `cmp-demo-714066696240-apne2-connection-templates`. 버킷 단위 공개 접근 차단 중 ACL 두 항목만 켜고 정책 공개는 허용. `BucketOwnerEnforced`, SSE-S3, 버전 관리(이전 버전 30일 뒤 만료), 스택 삭제 시 보존 |
| 버킷 정책 | `connections/<버전>/*`만 익명 `s3:GetObject`(현재 스크립트 버전 `v2`, 티켓 47 대상 템플릿 v2). 비TLS 요청은 모두 거부. 공개 쓰기·목록·ACL 없음. `v2`로 올리면 `v1` 객체는 더 이상 공개되지 않는다(이미 만든 스택은 영향 없음) |
| 템플릿 URL | `https://cmp-demo-714066696240-apne2-connection-templates.s3.ap-northeast-2.amazonaws.com/connections/v2/{source,target}-role.yaml` |
| AssumeRole 권한 | 빌드 스택 `cmp-demo-build`의 인라인 정책 `cmp-demo-connections`: `sts:AssumeRole`, 리소스 `arn:aws:iam::*:role/*`, 조건 `iam:ResourceTag/cmp:managed-by = cloud-migration-platform`(두 연결 템플릿이 역할에 붙이는 태그) + `sts:ExternalId`가 `cmp-*`로 있음. `cmp-demo-control-plane`은 건드리지 않는다 |
| api 설정 | `CONNECTION_PLATFORM_PRINCIPAL_ARN`(인스턴스 역할 ARN), `CONNECTION_TEMPLATE_BASE_URL`(위 접두어). `/opt/cmp/.env`, 비면 링크 없음. worker에는 주지 않는다(링크는 api만 만든다) |

템플릿 내용을 바꾸면 `deploy-connection-templates.sh`의 `version`(스택 `TemplateVersion`)을 `v2`로 올린다. 스크립트는 같은 버전에 이미 올라간 객체가 로컬 파일과 다르면 업로드하지 않고 실패한다.

### Provisioning의 연결 사용 (#181, 티켓 46)

| 항목 | 내용 |
| --- | --- |
| 생성 연결 설정 파일 | api `CONNECTION_AWS_CONFIG_FILE=/run/cmp/aws-connections/config`(호스트 `/opt/cmp/data/aws-connections`, api 쓰기·worker 읽기 전용), `CONNECTION_AWS_BASE_CONFIG_FILE=/run/cmp/aws/config`. 키는 없다(ARN·External ID·프로필 이름). api가 시작·역할 등록·연결 삭제 때 DB에서 다시 쓴다. `start.sh`의 `ensure-secrets.sh`가 없을 때만 기본 파일로 씨앗을 둔다 |
| 프로필 연쇄 | `cmp-conn-<hex>`(`credential_source = Ec2InstanceMetadata`) = 위 `CONNECTION_PLATFORM_PRINCIPAL_ARN`(인스턴스 역할)이 연결 역할을 바로 Assume한다. botocore는 `credential_source`만 있는 프로필(`cmp-demo-build`)을 `source_profile`로 받지 않으므로(`The source profile ... must have credentials`) 생성 프로필은 플랫폼 프로필의 `credential_source`를 그대로 옮겨 쓴다. 플랫폼 프로필이 키·역할·process를 가지면 지금처럼 `source_profile`이다 |
| worker | `AWS_CONFIG_FILE=/run/cmp/aws-connections/config`. Plan 입력 `credential_ref.id`가 연결 ID(`conn-<hex>`)면 provider·`aws`·kubectl은 `cmp-conn-<hex>`, State backend는 `PROVISIONING_AWS_PROFILE`. 아니면 지금처럼 `PROVISIONING_AWS_PROFILE` 하나 |
| api ECR 토큰 | `PLATFORM_ECR_AWS_PROFILE`(연결 주체 설정과 분리, 티켓 47부터 pull 전용 역할) |


### 반영 (각 단계 사용자 승인 필요)

```sh
bash infra/demo/deploy-build-stack.sh --plan             # 변경 세트: ConnectionAssumePolicy 추가 1건
bash infra/demo/deploy-connection-templates.sh --plan    # 변경 세트: 버킷·버킷 정책 추가
bash infra/demo/deploy-build-stack.sh                    # AssumeRole 권한 반영(.env 줄은 이미 반영돼 있으면 그대로)
bash infra/demo/deploy-connection-templates.sh           # 버킷 생성, 템플릿 2개 업로드(text/yaml), curl -sI 200 확인, .env 두 줄 출력
# SSM 세션에서 출력된 두 줄을 /opt/cmp/.env에 추가한 뒤
bash infra/demo/release.sh HEAD                          # 또는 GitHub Actions Deploy demo
```

### 서버 점검

```sh
# SSM 세션에서
docker exec cmp-demo-api-1 python -c 'from cmp_api.config import settings as s; print(s.connection_platform_principal_arn, s.connection_template_base_url)'
# 노트북에서
curl -sI https://cmp-demo-714066696240-apne2-connection-templates.s3.ap-northeast-2.amazonaws.com/connections/v2/source-role.yaml | head -1   # 200
curl -s -o /dev/null -w '%{http_code}\n' 'https://cmp-demo-714066696240-apne2-connection-templates.s3.ap-northeast-2.amazonaws.com/?list-type=2'   # 403 (목록 금지)
aws iam simulate-principal-policy --profile cmp-aws-prod \
  --policy-source-arn arn:aws:iam::714066696240:role/cmp-demo-control-plane-InstanceRole-oAXRipePmZCe \
  --action-names sts:AssumeRole --resource-arns arn:aws:iam::111122223333:role/x \
  --context-entries ContextKeyName=iam:ResourceTag/cmp:managed-by,ContextKeyValues=cloud-migration-platform,ContextKeyType=string \
                    ContextKeyName=sts:ExternalId,ContextKeyValues=cmp-0123456789abcdef0123456789abcdef,ContextKeyType=string \
  --query 'EvaluationResults[0].EvalDecision'   # allowed
docker exec cmp-demo-worker-1 sh -c 'grep -c "^\[profile cmp-conn-" "$AWS_CONFIG_FILE"'   # 역할이 등록된 AWS 연결 수 (티켓 46)
```

## 분석·Sandbox·ENV 실행 설정 (#180, 티켓 45)

웹 실제 시작(`POST /migrations`에 `target`)은 분석 → Sandbox → Planning 인계를 실제 Activity로 돈다. 데모 compose는 설정 **이름**만 연결하고 값은 `/opt/cmp/.env`에서 받는다. 비어 있으면 지금처럼 scaffold다. 넣을 수 있는 선택 줄은 [`runtime.env.example`](runtime.env.example)에 있다(값 없는 예시).

| 설정 | 서비스 | 기본 | 켜면 |
| --- | --- | --- | --- |
| `ENV_FILE_STORE_DIR`, `ENV_FILE_ENCRYPTION_KEY` | api·worker | 항상 켜짐(키 자동 생성) | `POST /env-files`, 실제 시작의 `env_file_ref` 고정, 분석 EnvPlan·`render_env`가 같은 저장소를 읽는다 |
| `ANALYSIS_ARTIFACT_STORE_DIR=/opt/cmp/artifact-store`, `ANALYSIS_WORKSPACE_ROOT=/opt/cmp/analysis-workspaces` | worker | 빈 값 → scaffold 분석 | 실제 분석 Activity 등록. **scaffold 시작(`target` 없음)은 분석에서 실패한다**(같은 Activity 이름, 인자 모양만 다름). 저장소는 `ARTIFACT_STORE_DIR`와 같아야 한다 |
| `SANDBOX_KUBECONFIG=/run/cmp/sandbox/kubeconfig` | worker | 빈 값 → scaffold Sandbox | 전용 `k3d-cmp-sandbox` 클러스터로 실제 루프. 파일은 `/opt/cmp/secrets/sandbox/kubeconfig`(root, 0600). 경로만 있고 파일이 없으면 worker가 시작하지 못한다 |
| `SANDBOX_PREBUILT_IMAGES` | worker | 빈 값 | 실제 루프의 앱 이미지(Digest 고정 JSON). `BUILD_*`가 모두 있으면 대신 실제 빌드 |
| `LLM_ENABLED=true`, `ANTHROPIC_API_KEY` | worker | 꺼짐 | Claude 사용. 원장 `/opt/cmp/data/llm/ledger.json`, 즉시 중지 `touch /opt/cmp/data/llm/disabled` |
| `AGENT_CHAT_ENABLED=true` | api | 꺼짐(웹이 예전처럼 웹 서버에서 직접 묻는다) | 에이전트 "자칼"의 질문 답을 플랫폼 api가 만든다(`POST /agent/ask`, 웹 서버만 부른다·공개 엣지에 없음). 진행 신호(상태 읽는 중 → 모델에 묻는 중 → 자막 옮기는 중)를 SSE로 보내고, 모델 호출은 위 `LLM_*` 비용 장부·긴급 중지(`touch /opt/cmp/data/llm/disabled`)를 같이 쓴다. 켜면 api에 `ANTHROPIC_API_KEY`가 전달되고 `/opt/cmp/data/llm`이 마운트된다. 모델은 `AGENT_CHAT_MODEL`(기본 `claude-haiku-5-5`). 웹 서버에는 `PLATFORM_AGENT_URL`(api 내부 주소, 예 `http://api:8000`)을 준다 |
| `PULSE_REPORT_TOKEN=<random>` | api | 꺼짐 | 원본·이전 서버가 CPU·메모리를 `POST /system/pulse/report`로 보낼 때 쓰는 공유 토큰(헤더 `X-Pulse-Token`). 비면 수신을 끈다. 리포터는 `infra/demo/pulse-reporter.py`. 중계 서버의 원본·대상 건강 지표(`POST /system/traffic-health/report`, 5xx 비율과 p99)도 같은 토큰을 쓴다. 리포터는 `python3 -m cmp_traffic.health`(`infra/relay/traffic-health.service`) |
| `PLANNING_TARGET_PROFILES_FILE=/opt/cmp/config/target-profiles.json` | api | 파일 없음 → 실제 시작 503 `TARGET_PROFILES_UNCONFIGURED` | 웹 `target.profile`이 고르는 대상 설정(44) |
| `WEB_START_SANDBOX_SETTINGS` | api | 빈 값 → 리허설은 헬스 테스트만 | 웹 실제 시작의 `sandbox`(SandboxSettings JSON 객체, CLI `--sandbox`와 같다)로 표준 DB 쓰기→읽기·재시작 후 유지 테스트까지 돈다(티켓 57). 데모 앱(demo-app) 값은 `runtime.env.example`의 줄 그대로(작은따옴표 유지). JSON 객체가 아니면 실제 시작이 503 `SANDBOX_SETTINGS_INVALID`. 다른 앱은 그 앱의 쓰기 모양을 따로 적는다 |
| `WEB_START_DATA_MIGRATION` | api | 빈 값 → 데이터 단계는 scaffold | 웹 실제 시작의 `data_migration`(DataMigrationInput JSON 객체, CLI `--data-migration`과 같다)으로 구축 뒤 CDC 복제·DB 전환 승인(`approve?gate=cutover`)까지 실제로 돈다(data-migration 티켓 08). 원본 DB·앱·전환 설정·대상 터널 주소만 담고 비밀값은 없다. 검증에 실패하면 실제 시작이 503 `DATA_MIGRATION_SETTINGS_INVALID`. worker에 kubectl과 원본·대상 클러스터 접근(EKS·GKE 자격 증명 또는 `DATA_MIGRATION_KUBECONFIG_DIR`)이 있어야 한다. `tunnel: {"source_egress_cidrs": ["<원본 NAT IP>/32"]}`을 넣으면 플랫폼이 두 클러스터 사이 WireGuard 게이트웨이를 만들고 주소를 정한다(data-migration 티켓 10, 이때 원본 `peer_host`는 생략하고 `local_host`는 필수). **ON_PREM(ADR-007 2안, Provisioning 76)**: `tunnel: {"ingress": "source", "target_egress_cidrs": ["<맥 공인 IP>/32"]}`, `target_kubeconfig_ref: "<ref>"`(`DATA_MIGRATION_KUBECONFIG_DIR/<ref>.yaml`), `network_preflight: {"aws_vpc_cidrs": ["<원본 VPC CIDR>"], "db_auth_ref": {"secret_name": "<검사 Secret>", "username_key": "username", "password_key": "password"}}`(선택: `allowed_private_destination_cidrs` 기본 `10.43.0.0/16`, `onprem_routed_cidrs`, `tunnel_cidrs`, `tls_mode` 기본 `disable`, `probe_namespace`, `node_name`). 검사 Secret은 대상 DB Namespace에 운영자가 미리 둔다(원본 DB 읽기 계정, 값은 출력하지 않는다). ON_PREM + `ingress=source`인데 `network_preflight`가 없으면 503 `DATA_MIGRATION_PREFLIGHT_REQUIRED`. `tunnel.standing_ingress: true`(티켓 82)면 미리 만든 고정 NLB를 재사용하고 정리 때 남긴다(아래 절). 아니면 시작하면 원본 EKS에 공개 UDP NLB를 만들고 handshake까지(약 2분 30초) 기다린 뒤 사전 점검 PASS일 때만 복제하며, 실행이 끝나면(finish·실패·취소·차단) NLB와 게이트웨이를 지운다 |
| `DATA_MIGRATION_AWS_PROFILE=cmp-demo-data-migration` | worker | 빈 값 → EKS 데이터 접근은 기본 자격으로 시도하다 실패(권한 없음) | 데이터 이전 Activity가 원본 EKS에 이 프로필로 접속한다(Provisioning 72). `deploy-data-migration-stack.sh` 뒤 `DATA_MIGRATION_ROLE_ARN` 줄과 함께 넣는다 |
| `DATA_MIGRATION_KUBECONFIG_DIR=/run/cmp/data-migration` | worker | 빈 값 → `KUBECONFIG` 접근은 `DbAccessNotConfigured` | `access: KUBECONFIG` DB(온프레미스 k3d)를 `<dir>/<kubeconfig_ref>.yaml`로 연다. 파일은 `/opt/cmp/secrets/data-migration/`(root, 0600, 읽기 전용 마운트). 권한을 좁힌 ServiceAccount 토큰만 쓴다(ADR-007). 사용자 맥에서 `python3 infra/onprem-agent/make-data-kubeconfig.py apply --context <ctx> --namespace <대상 ns> --server https://<중계 서버 사설 IP>:<포트> --ref <ref>`로 만든다([온프레미스 설치 §8](../../specs/provisioning/onprem-agent-install.md), 티켓 75). 서버 배치는 승인 뒤 SSM 세션에서 위 경로(root, 0600)로 둔다(내용을 화면에 출력하지 않는다). 확인은 `docker compose exec worker kubectl --kubeconfig /run/cmp/data-migration/<ref>.yaml get --raw /readyz` |
| `WEB_START_TRAFFIC` | api | 빈 값 → 트래픽 단계는 scaffold(중계 서버를 건드리지 않는다) | 웹 실제 시작의 `traffic`(TrafficShiftInput JSON 객체, CLI `--traffic`과 같다)으로 데이터 전환 뒤 중계 서버(Envoy) 가중치를 10→50→100%로 실제로 옮긴다(#128). 중계 서버 워커 큐(`relay_queue`, 데모 중계 서버는 `relay-1`)와 단계·게이트 기준만 담고 비밀값은 없다. 예: `{"relay_queue":"relay-1"}`(나머지는 기본 정책). 검증에 실패하면 실제 시작이 503 `TRAFFIC_SETTINGS_INVALID`. 중계 서버 워커가 떠 있고 Provisioning 인계값 `external_endpoint`가 중계 서버에서 닿아야 한다. 100% 뒤 최종 확정 전 안정화 구간을 두려면 `policy.stabilize_seconds`(예: `{"relay_queue":"relay-1","policy":{"stabilize_seconds":1800}}`)를 준다: 대상 오류율·p99가 기준을 넘으면 `traffic.alert`로 알리고, 사람이 `POST /migrations/{id}/traffic/rollback`(트래픽과 DB를 원본으로) 또는 `/finalize`(확정)를 누를 때까지 기다린다. 시연용 장애는 중계 서버에서 `FAULT_TOKEN=<대상 앱과 같은 값> python3 -m cmp_traffic.fault on|off|status` |

**매핑(plan.step)과 Claude**: 분석 매핑 행은 Claude(mapper)가 만든다. `LLM_ENABLED`가 꺼져 있거나 `ANTHROPIC_API_KEY`가 비어 있으면 매핑이 돌지 않아(`MAPPING_NOT_RUN: LLM_NOT_CONFIGURED`) `plan.step`이 0건이고 웹의 「매핑 근거」·「구성도」가 비며, `plan.preview`의 `warnings`에 그 코드가 실린다. 켜려면 `/opt/cmp/.env`에 두 이름을 넣고(값은 출력·커밋하지 않는다) worker를 다시 만든다(`docker compose up -d worker`). compose가 두 값과 원장(`LLM_LEDGER_PATH`)·중지 스위치(`LLM_KILL_SWITCH_PATH`)를 worker에 넘기고, 호스트 `/opt/cmp/data/llm`(start.sh가 만든다)이 `/opt/cmp/llm`으로 붙는다. 켠 뒤 `runtime-check.sh`의 `optional_unset`에서 두 이름이 빠지고, worker 사전 점검의 `llm`이 WARN에서 OK가 된다. `disabled` 파일이 있으면 켜도 돌지 않는다.

지금 데모 서버에는 Sandbox 클러스터가 없다(분석 D-08: Sandbox는 로컬 k3d). 그래서 분석만 실제로 켜면 실제 시작은 Sandbox 첫 단계(`sbx_build`)에서 멈춘다. 어떤 경로를 켤지는 박승철님과 정한다(티켓 45 협의 필요).

**운영자 키와 웹**: `/env-files`는 `/connections`·`/agent-enrollments`와 같은 `X-Enrollment-Admin-Key`(= `AGENT_ENROLLMENT_ADMIN_KEY`)를 요구한다(`APPROVAL_ADMIN_KEY` 아님). compose는 이 키를 웹에 넣지 않는다. 웹 실제 모드에서 연결·`.env` 업로드·실제 시작을 쓰려면 운영자가 `/opt/cmp/web.env`에 `PLATFORM_ENROLLMENT_ADMIN_KEY=<AGENT_ENROLLMENT_ADMIN_KEY와 같은 값>`을 둔다. 웹이 그 경로를 로그인으로 막은 뒤에만 한다(위 「공개 HTTPS 에지」의 이유).

### 반영 (각 단계 사용자 승인 필요)

```sh
# 1. 이 커밋을 main에 병합한 뒤 GitHub Actions Deploy demo(또는 bash infra/demo/release.sh HEAD).
#    start.sh의 ensure-secrets.sh가 ENV_FILE_ENCRYPTION_KEY를 없을 때만 만들고 /opt/cmp/data/{env-files,analysis-workspaces,llm}을 만든다.
# 2. (SSM 세션) 카탈로그. 결선 데모 기본은 ON_PREM 하나뿐인 target-profiles.demo.json이다(웹 기본 대상
#    onprem-k3s-basic). GCP·AWS 시작은 분석 뒤가 아니라 시작에서 422 TARGET_PROFILE_NOT_FOUND로 거절된다.
#    AWS_TARGET 연결·GCP 키가 준비된 뒤에만 예시(target-profiles.example.json)를 복사해 실제 계정·프로젝트·State 값을 넣는다. 비밀 없음.
sudo install -d -m 755 /opt/cmp/config
sudo install -m 644 /opt/cmp/releases/<SHA>/infra/demo/target-profiles.demo.json /opt/cmp/config/target-profiles.json
# 3. (SSM 세션, 선택) 빌드 값: deploy-build-stack.sh가 출력한 다섯 줄(PLATFORM_ECR_REGISTRY, BUILD_*)을 /opt/cmp/.env에 추가.
# 4. (SSM 세션, 선택, 협의 후) 분석·Sandbox·Claude 줄을 runtime.env.example에서 골라 /opt/cmp/.env에 추가.
#    Sandbox는 kubeconfig를 /opt/cmp/secrets/sandbox/kubeconfig(root, 0600)에 먼저 둔다.
# 5. .env를 바꿨으면 다시 릴리스한다(컨테이너가 .env를 다시 읽는다). 카탈로그만 바꿨으면 재시작 불필요(요청마다 읽는다).
bash infra/demo/release.sh HEAD
```

### 서버 점검 (티켓 45)

SSM 세션에서 root로 실행한다. 아무것도 바꾸지 않고, `.env`는 **이름만** 출력한다.

```sh
sudo bash /opt/cmp/releases/$(cat /opt/cmp/current-revision)/infra/demo/runtime-check.sh
```

| 줄 | 기대 |
| --- | --- |
| `revision=` | 배포한 main 커밋 앞 12자 |
| `container=` | api·worker·temporal·DB가 `Up`, DB는 `(healthy)` |
| `alembic=` | 그 커밋의 head(`… (head)`) |
| worker `analysis=`·`sandbox=`·`build=`·`llm=`·`env_store=`·`platform_ecr_registry=` | `real`/`scaffold`, `on`/`off`. 빌드를 켰으면 `platform_ecr_registry=on`이어야 한다(api와 같은 값; `off`면 ON_PREM rollout이 `IMAGE_REGISTRY_NOT_ALLOWED`). `error:<CODE>`면 설정이 잘못됐다(예: `BUILD_SETTINGS_INCOMPLETE`, `SANDBOX_KUBECONFIG_NOT_FOUND`) |
| api `env_store=`·`target_profiles=`·`connection_link=`·`platform_ecr_registry=`·`web_start_sandbox=` | `on`, 카탈로그 이름 목록(`error:TARGET_PROFILES_UNCONFIGURED`면 파일 없음), 링크·레지스트리 `on`, 리허설 기본 모양 `on`(`error:SANDBOX_SETTINGS_INVALID`면 JSON이 아니다) |
| `imds_guard_rules=1`, `imds_guard_service=enabled` | 티켓 54 빌드 IMDS 차단 |
| `missing=` | 비어 있어야 한다(compose 필수 키) |
| `optional_unset=` | 아직 켜지 않은 선택 기능 이름 |
| `target_profiles_file=`·`sandbox_kubeconfig_file=`·`gcp_key_file=` | 배치 여부(`present`/`absent`) |
| `data_migration_kubeconfig_files=` | `/opt/cmp/secrets/data-migration`의 `*.yaml` 개수(이름은 출력하지 않는다) |

## 접속과 확인

SSM Session Manager의 포트 포워딩으로 로컬 `8000`을 EC2 `8000`에 연결하면 `/health`와 `/docs`를 볼 수 있다. Temporal UI는 같은 방식으로 `8080`에 접속한다. API/Temporal UI는 EC2의 loopback에만 바인딩된다. 공개 URL은 Agent·중계·이전·Plan 승인 경로(`/agents/*`, `/relays/*`, `/migrations[/*]`, `/provisioning/*`)만 API로 전달하는 에지뿐이다.

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
