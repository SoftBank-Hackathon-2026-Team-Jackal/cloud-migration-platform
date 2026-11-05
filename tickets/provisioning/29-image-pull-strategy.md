# Ticket 29 — 대상별 ECR 이미지 pull 전략 (GKE 갱신·K3s pull Secret)

## 상태
IN_PROGRESS (로컬 구현·테스트 완료, 조율 세션 리뷰 대기) — [Issue #153](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/153). 2026-11-06 조율 세션 지시로 착수 결정을 기록하고 구현했다. **새 Agent 릴리스가 필요하다(사용자 승인 뒤 태그, 이 티켓에서 태그하지 않음).**

## 목적
플랫폼이 빌드한 이미지(28, 플랫폼 ECR)를 세 대상이 사람 개입 없이, 12시간이 지나도 받을 수 있게 한다.
- EKS: 노드 역할 `AmazonEC2ContainerRegistryPullOnly`(08 Terraform)로 받는다. 변경 없음.
- GKE: 11 K4는 12시간짜리 ECR pull Secret을 실행 때 한 번 넣고 갱신하지 않는다. 12시간 뒤 Pod가 재스케줄되면 pull이 실패한다.
- K3s(ON_PREM): Agent가 pull Secret을 만들지 않아 private ECR 이미지를 받을 수 없다. pull 실패도 `WORKLOAD_NOT_READY`로만 보고된다(PROV-010).

로컬 실행이 사용자 관리자 프로필을 써서 드러나지 않았다.

## 관련 요구사항
PROV-009, PROV-010, PROV-013, PROV-018, PROV-021

## 선행 작업
Blocked by: 11, 12, 15, ENV-04 (DONE). 28(플랫폼 저장소 `cmp-platform-builds`·인스턴스 역할 권한)과는 설정 값으로만 연결한다(파일 의존 없음).

## 원칙 (결정됨)
배포할 이미지는 **플랫폼 ECR**에 있다. AWS 권한은 플랫폼만 갖는다. 플랫폼이 자기 역할로 짧은 ECR 인증 토큰을 발급하고, 실행 주체(K3s는 Agent, GKE는 플랫폼 Worker)가 pull Secret을 만들고 만료 전에 갱신한다. 노트북(K3s)에는 AWS 키를 두지 않는다. EKS는 노드 역할을 그대로 쓴다. 고객 계정 EKS의 교차 계정 ECR 저장소 정책은 후속(미구현)이다.

## 범위
- 포함: 플랫폼 토큰 발급 모듈, Agent 작업별 pull 자격 API, Agent pull Secret 생성·주기 갱신·충돌 거부, ON_PREM `IMAGE_PULL_FAILED`, Agent Chart RBAC, GKE 주기 갱신(플랫폼 측 Temporal Schedule), SDD·OpenAPI
- 제외: 실제 AWS/GCP/K3s 호출, Agent 릴리스 태그, 교차 계정 ECR 정책, Artifact Registry 미러, 28의 저장소·IAM 생성

## 수용 기준 (Issue #153)
- [x] K3s·GKE에서 private ECR 이미지를 사람 개입 없이 받고, 12시간이 지나도 다시 받을 수 있다. (가짜 ECR·클러스터·Agent로 확인, 실제 환경 미검증)
- [x] 토큰·Secret 값이 작업 본문·로그·이력에 없다.
- [x] ON_PREM pull 실패가 `IMAGE_PULL_FAILED`로 보고된다.

## Test Seam
- 토큰 발급: `cmp_providers.ecr_pull.EcrTokenMinter`에 주입한 `botocore` Stubber ECR 클라이언트
- Agent API: 12의 실제 ON_PREM 승인 Revision(`onprem_rollout_support`) + FastAPI `TestClient` + 의존성 주입한 가짜 Minter
- Agent: 12의 `FakeCluster`·`FakeClient`와 `RolloutRunner`/`PullSecretRefresher`(가짜 시계)
- GKE: 11의 `FakeRuntime`·`FakeCluster`와 실제 `execute_gcp_workloads`/`refresh_gcp_pull_secrets`(22 게시 경로)
- 놓치는 것: 실제 ECR 토큰·kubelet pull, 실제 Temporal Schedule 서버 동작(테스트 서버로 Workflow만 확인), 실제 Agent 이미지

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| I1 레지스트리 설정 | 플랫폼 레지스트리는 설정으로 정한다: `PLATFORM_ECR_REGISTRY`(`<12자리 계정>.dkr.ecr.<region>.amazonaws.com`), `PLATFORM_ECR_REPOSITORIES`(쉼표 구분, 기본 `cmp-platform-builds`). 형식이 틀리면 `PLATFORM_REGISTRY_INVALID`. 승인된 DB·앱 이미지 중 ECR URI가 있으면 그 레지스트리·저장소가 설정과 같아야 한다. 아니면 ON_PREM 작업 생성이 `IMAGE_REGISTRY_NOT_ALLOWED`로 멈춘다(설정이 없을 때도 같은 코드). ECR이 아닌 이미지만 있으면 pull Secret 없이 이전 경로 그대로다 | 28이 저장소·역할을 병렬로 만든다. 파일 의존 없이 값만 맞춘다. 다른 계정 ECR은 플랫폼 역할로 받을 수 없으므로 실행 전에 거절한다 |
| I2 토큰 발급 | `cmp_providers.ecr_pull`: 플랫폼 주체(`CONNECTION_PLATFORM_AWS_PROFILE`, 데모는 인스턴스 역할)로 `ecr:GetAuthorizationToken`을 부른다. 응답 `proxyEndpoint`가 `https://<설정 레지스트리>`와 다르면 `ECR_ACCOUNT_MISMATCH`(플랫폼 계정만). 실패는 `ECR_TOKEN_UNAVAILABLE`. 토큰은 `PullCredential` 메모리에만 있고 `repr`·로그·결과·Temporal 이력에 나오지 않는다 | `registryIds`는 폐기 예정이고 토큰은 호출 주체 계정 기준이다. CLI 대신 boto3로 API 프로세스에서 바로 쓴다 |
| I3 Agent pull 자격 API | `GET /agents/{agent_id}/jobs/{job_id}/image-pull` → `{registry, username, password, expires_at}`(`Cache-Control: no-store`). ENV-04 E6과 같은 Agent 인증·자기 작업 검사(다른 Agent 403, 남의 작업 404). 허용 상태: `RUNNING`, 또는 `SUCCEEDED`이면서 승인 검사(`check_dispatchable`)가 아직 통과하는 작업(최신 승인 Revision = 지금 배포된 것). 레지스트리는 작업 본문이 아니라 승인된 Values에서 다시 계산해 작업의 `image_pull.registry`와 같아야 한다. 아니면 404 `IMAGE_PULL_NOT_FOUND`, 설정·발급 실패는 503 + 코드 | RUNNING만 허용하면 배포 뒤 갱신(12시간 후 재pull)을 할 수 없다. 새 Revision이 승인되면 이전 작업은 `STALE_REVISION`으로 막혀 갱신이 멈춘다 |
| I4 작업 계약 | `ProductionRolloutJob.image_pull: {secret: "cmp-ecr-pull", registry}`(선택, 비어 있으면 직렬화에서 빠짐 → 기존 작업 바이트 동일). 이전 Agent는 새 필드가 있는 작업을 `UNSUPPORTED_JOB`으로 즉시 거절한다(멈추지 않음) | ENV-04 `env_secrets`와 같은 하위 호환 방식 |
| I5 Agent Secret | Agent는 Helm 전에 자격을 받아 대상 Namespace에 `kubernetes.io/dockerconfigjson` Secret `cmp-ecr-pull`을 만들거나 갱신한다(stdin apply, 라벨 `cmp.dev/managed=true`·`cmp.dev/purpose=image-pull`, 비밀 아닌 주석 `cmp.dev/job-id`·`cmp.dev/pull-issued-at`·`cmp.dev/pull-expires-at`). 같은 이름의 관리 밖 Secret이 있으면 변경 전에 `SECRET_NAME_CONFLICT`. Namespace `default` ServiceAccount의 `imagePullSecrets`에 연결한다(11 K4와 같음, 두 Chart가 ServiceAccount를 지정하지 않아 앱·DB 모두 적용, Chart 무변경). 받지 못하면 `IMAGE_PULL_CREDENTIALS_UNAVAILABLE`(UNKNOWN), 응답이 작업과 다르면 `IMAGE_PULL_CREDENTIALS_INVALID`, Agent에 기능이 없으면 `IMAGE_PULL_NOT_CONFIGURED` | 승인된 Values를 바꾸지 않고 pull 자격을 붙인다 |
| I6 Agent 주기 갱신 | Agent 데몬은 poll 주기마다 `PullSecretRefresher`로 클러스터의 관리 pull Secret(`cmp.dev/purpose=image-pull`)을 조회하고, 발급 뒤 TTL의 절반이 지난 것을 같은 API로 다시 받아 갱신한다. 상태를 Secret 주석에 두므로 Agent 재시작 뒤에도 이어진다. 403은 데몬의 `AGENT_UNAUTHORIZED` 처리(Agent 중지). 그 밖의 실패(새 Revision 승인으로 404 등)는 코드·Namespace만 기록하고 그 Secret을 5분 뒤 다시 시도한다(매 poll 호출 방지). 기존 Secret은 만료까지 유효하다 | Agent는 이미 상시 실행(15). CronJob은 클러스터에 AWS 자격이 필요해 쓰지 않는다 |
| I7 `IMAGE_PULL_FAILED` | Workload가 Ready가 아닐 때 그 Release의 Pod(`app.kubernetes.io/instance`) 대기 사유가 `ErrImagePull`·`ImagePullBackOff`·`InvalidImageName`·`ErrImageNeverPull`이면 `IMAGE_PULL_FAILED`(FAILED), 아니면 `WORKLOAD_NOT_READY` | 09/11의 `PULL_FAILURES`와 같은 집합 |
| I8 Agent RBAC | ClusterRole에 `serviceaccounts`: `get`, `create`, `patch`만 추가(기본 SA 연결). Secret은 이미 15 H2 권한(get·list·create·update·patch)으로 충분하다 | 최소 권한. 새 Namespace의 기본 SA가 아직 없을 때만 create가 필요하다 |
| I9 GKE 재실행 | 11 K4의 `prepare_namespace`는 실행마다 Secret을 새 토큰으로 server-side apply한다(재실행 = 갱신, 테스트로 고정). 토큰 출처는 Worker의 플랫폼 AWS 프로필(`PROVISIONING_AWS_PROFILE`)과 STS 계정 일치 검사 그대로다 | 26이 같은 배포 Activity를 바꾸므로 변경을 새 함수에 둔다 |
| I10 GKE 주기 갱신 | 플랫폼 측 Temporal Schedule `cmp-gcp-ecr-pull-refresh`(6시간, 겹침 건너뜀) → `GcpPullSecretRefreshWorkflow` → Activity `refresh_gcp_pull_secrets`. Activity는 `SUCCEEDED`이고 refs에 `image_pull_secret`이 있는 GCP 워크로드 실행을 찾아 실행 때와 같은 승인 재검증(`_load`, 최신 승인 Revision만)·GCP/AWS 계정 확인 뒤 pull Secret만 다시 apply한다(Helm·Values 변경 없음). 결과는 개수·코드만. Schedule은 Provisioning Worker 시작 때 없으면 만든다 | CronJob은 클러스터에 AWS 자격이 필요해 금지. 6시간은 12시간 TTL의 절반이다. MigrationWorkflow를 바꾸지 않아 기존 이력 재생에 영향이 없다 |
| I11 후속 | 고객 계정 EKS의 교차 계정 ECR 저장소 정책, GKE 경로의 저장소 허용 목록(I1) 적용, 실제 환경 검증, Agent 릴리스 | 범위 밖 기록 |

## 구현 위치
- 토큰: `packages/providers/src/cmp_providers/ecr_pull.py`
- API: `cmp_api.onprem_rollout.image_pull_registry`·`job_image_pull`, `cmp_api.routes.agents` `/image-pull`, 설정 `platform_ecr_registry`·`platform_ecr_repositories`
- 계약: `cmp_contracts.onprem_rollout.RolloutImagePull`, `ProductionRolloutJob.image_pull`
- Agent: `cmp_providers.agent_image_pull`(Secret·SA·갱신·pull 실패 판별), `onprem_agent`(호출 지점만), `remote_agent.image_pull`, `agent_runtime`(poll마다 갱신)
- GKE: `cmp_worker.activities.gcp_workloads`(`refresh_gcp_pull_secrets`), `cmp_worker.workflows.pull_refresh`, Worker 등록·Schedule 보장
- Chart: `infra/helm/onprem-agent/templates/rbac.yaml`

## 검증
- `tests/test_image_pull.py`(33), `make check` 1772 passed·8 skipped, `make openapi` 검토(새 경로 `/agents/{agent_id}/jobs/{job_id}/image-pull`·`ImagePullCredential` 스키마만 추가)
- DB 모델 변경 없음. `alembic check`는 로컬 Postgres가 없어 실행하지 못했다(테스트 안의 Alembic·모델 일치 검사는 통과). `make smoke-temporal`은 로컬 인프라가 없어 미실행
- 기존 테스트 조정: 18의 Planning 이미지가 ECR(`123456789012…/demo`)이라 `test_plan_publish`에 플랫폼 레지스트리 설정 fixture를 추가했다(I1)
- 운영 반영 시: API 설정 `PLATFORM_ECR_REGISTRY`·`PLATFORM_ECR_REPOSITORIES`, 플랫폼 역할에 `ecr:GetAuthorizationToken`과 저장소 `ecr:BatchGetImage`·`ecr:GetDownloadUrlForLayer`(28 범위), 새 Agent 이미지·Chart(RBAC) 배포
