# Sandbox/온프레미스 공통 배포 입력·렌더 결과 계약

> 아래의 `SharedDeploymentRequest`/`Result` 예시는 **구현된 v1 단일 앱·DB 계약**이다. 다중 리소스 v2는 이 문서의 **v2 공통 렌더 입력·반환** 절과 `render_shared_deployment_v2`를 따른다. 두 호출은 별도이며 v1 입력을 자동 승격하지 않는다.

## 상태와 책임

2026-11-03 사용자와 합의한 **공통 렌더 계약**이다. 04A(#20)의 `SharedDeploymentRequest`/`SharedDeploymentResult`와 `cmp_providers.shared_deployment.render_shared_deployment`가 기존 `PostgresChartValues`, `ApplicationValues`, DB 바인딩 및 두 Helm Chart를 조합한다. 이 문서는 실제 배포가 끝났다는 뜻이 아니다.

Sandbox Worker는 미리 준비된 별도 클러스터의 임시 Namespace에 적용·검증·정리한다. Provisioning Worker는 승인된 운영 계획에 한해서 사용자 제공 K3s의 배포를 요청하고 결과를 확인한다. 공통 모듈은 입력 검증, 이름·연결값 계산과 DB/App Values·Manifest 렌더링을 수행한다. 클러스터 접속, Secret 생성·조회, Kubernetes 적용, 테스트, Ready 판정, 정리는 Worker/Agent 책임이다. Sandbox는 Production Provisioning Workflow 전체를 호출하지 않는다.

## 공통 입력

| 필드 | 역할 |
| --- | --- |
| `environment`, `provider` | `SANDBOX` 또는 `PRODUCTION`, 대상 `AWS`/`GCP`/`ON_PREM`. 온프레미스 운영은 `PRODUCTION` + `ON_PREM`이다. |
| `namespace`, `release_names.app/db`, `storage_class` | 배포할 Namespace, 별도 앱/DB Release, DB PVC의 StorageClass. Cluster 접속 정보는 포함하지 않는다. |
| `app` | Digest 고정 이미지·지원 플랫폼, 복제 수, CPU millicores·메모리 MiB, 컨테이너 Port, Health Path, 설정 산출물 참조. |
| `db` | PostgreSQL 16 Digest 고정 이미지·지원 플랫폼, DB 이름/사용자, 비밀번호 Secret 이름·키, CPU·메모리, PVC GiB. 비밀번호 원문은 포함하지 않는다. |
| `env_bindings` | 앱의 필수 환경변수 이름과 `CONFIG_MAP`/`USER_SECRET`/`DATABASE` 출처. 일반 설정·Secret은 이름/키만, DB 변수는 host/port/name/username/secret_ref 필드로 연결한다. |

예시(`sha256`·설정 참조는 설명용):

```yaml
contract_version: "1"
environment: SANDBOX
provider: ON_PREM
namespace: cmp-test-001
release_names: {app: cmp-test-001-app, db: cmp-test-001-db}
storage_class: local-path
app:
  image:
    uri: "registry.example.com/demo-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    platforms: [linux/arm64]
  config_ref: {id: config-001, version: "1", sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}
  replicas: 1
  cpu_millicores: 250
  memory_mib: 256
  port: 8080
  health_path: /health
db:
  image:
    uri: "docker.io/library/postgres@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    platforms: [linux/arm64]
  version: "16"
  name: appdb
  username: appuser
  password_secret_name: cmp-test-001-db-auth
  password_secret_key: password
  cpu_millicores: 250
  memory_mib: 512
  pvc_size_gib: 5
env_bindings:
  APP_MODE: {kind: CONFIG_MAP, name: app-config, key: mode}
  API_KEY: {kind: USER_SECRET, name: uploaded-secrets, key: api-key}
  DB_HOST: database.host
  DB_PORT: database.port
  DB_NAME: database.name
  DB_USER: database.username
  DB_PASSWORD: database.secret_ref
```

입력은 현재 두 Chart 모델의 공통 필드를 조합한다. `config_ref`와 이미지 `platforms`는 현재 앱 Chart 계약에서 필수라 예시에 명시했다. `env_bindings`는 변수 이름을 키로 사용하고 DB 값은 `database.host` 등 필드 이름, 일반·사용자 Secret은 `kind`/`name`/`key` 참조를 받는다. 실제 Secret·ConfigMap 존재 및 이미지 Pull 권한은 이 모듈이 보증하지 않는다. SANDBOX에 운영 Credential을 요구하지 않는다.

## 공통 반환

| 필드 | 의미 |
| --- | --- |
| DB/App Helm Values 및 렌더 Manifest | 호출자가 저장·Digest 검증 후 Worker/Agent에 전달할 정확한 배포 내용. DB/App Chart·Release를 분리한다. |
| 내부 DB Service `host`, `port` | Release·Namespace에서 계산한 연결 대상. 실제 DNS/DB 접근 성공의 증거는 아니다. |
| 앱 환경변수 매핑 | `DB_HOST` 등은 계산한 DB 정보로, DB 비밀번호는 Secret 이름/키 참조로 연결한다. 원문 값은 포함하지 않는다. |
| 검증 결과 | 성공 시 유효한 렌더 결과, 실패 시 필드/코드가 있는 오류. `READY`나 배포 완료 상태는 반환하지 않는다. |

```yaml
environment: SANDBOX
namespace: cmp-test-001
database:
  release_name: cmp-test-001-db
  helm_values: "<최종 DB Values>"
  manifest_yaml: "<StatefulSet·Service·PVC YAML>"
  service: {host: cmp-test-001-db.cmp-test-001.svc, port: 5432}
application:
  release_name: cmp-test-001-app
  helm_values: "<DB 주소가 채워진 앱 Values>"
  manifest_yaml: "<Deployment·Service YAML>"
bindings:
  DB_HOST: cmp-test-001-db.cmp-test-001.svc
  DB_PORT: "5432"
  DB_NAME: appdb
  DB_USER: appuser
  DB_PASSWORD: {secret_name: cmp-test-001-db-auth, secret_key: password}
validation: {passed: true, errors: []}
```

큰 Manifest는 모듈 내부에서 정확한 바이트로 생성한다. S3 참조 발급·저장, 승인 Artifact/Digest와의 대조, 클러스터 배포 및 실제 Ready/DB/앱 검증은 호출자가 맡는다. Sandbox Worker 테스트 결과와 Production Worker의 `ProvisioningResult`는 이 렌더 결과와 별도로 기록한다.

공개 함수 호출은 다음과 같다. 입력은 위 YAML을 파싱한 `dict`이며 반환 모델 안에 DB/App Values와 Manifest YAML 문자열이 들어 있다. 실패 시 `ProvisioningInputError.issues`에 비밀값 없는 오류 코드가 들어간다. Helm CLI 호출은 외부 I/O이므로 Temporal Workflow 내부가 아닌 Activity/Worker 서비스에서 실행한다.

```python
from pathlib import Path
from cmp_providers.shared_deployment import render_shared_deployment

result = render_shared_deployment(
    payload,
    database_chart_dir=Path("infra/helm/postgres"),
    application_chart_dir=Path("infra/helm/application"),
)
```

## v2 공통 렌더 입력·반환

`cmp_providers.shared_deployment_v2.render_shared_deployment_v2`는 Sandbox Worker와 Provisioning Worker가 **대상 환경에 맞는 구체값을 결정한 뒤** 호출하는 공통 렌더 경계다. Planning의 `RequirementsV2.resources[]`는 분석 요구이며 다음 입력의 Release 이름, 이미지, StorageClass, Secret 참조까지 확정한 상태가 아니다. Worker는 `.env`와 `mapping_ref`의 제외·대체 규칙을 해석하고 Secret을 준비해 최종 변수별 바인딩만 전달한다. 원문 비밀값은 렌더러에 넣지 않는다. 입력·반환 모델은 `cmp_contracts.shared_deployment_v2`에 정의했다.

```yaml
contract_version: "2"
environment: SANDBOX           # 운영 배포는 PRODUCTION
provider: ON_PREM              # 검증하는 대상 Provider
namespace: cmp-test-001        # 호출 Worker가 결정
resources:
  - id: postgres-main
    kind: POSTGRESQL
    release_name: cmp-test-001-db
    image: {uri: "docker.io/library/postgres@sha256:<Digest>", platforms: [linux/amd64]}
    version: "16"               # Analysis 요구와 이미지 호환 확인
    extensions: []              # P0 미지원 확장은 거절
    database:
      name: appdb
      username: appuser
      password_secret_ref: {name: cmp-test-001-db-auth, key: password}
    resources: {cpu_millicores: 250, memory_mib: 512}
    storage: {class_name: local-path, size_gib: 5}
  - id: api-main
    kind: APPLICATION
    release_name: cmp-test-001-app
    image: {uri: "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/demo@sha256:<검증 Digest>", platforms: [linux/amd64]}
    replicas: 1
    resources: {cpu_millicores: 250, memory_mib: 256}
    port: 8080
    health_path: /health
    env_bindings:
      DB_HOST: {resource_id: postgres-main, output: host}
      DB_PORT: {resource_id: postgres-main, output: port}
      DB_PASSWORD: {resource_id: postgres-main, output: password_secret_ref}
      API_KEY: {secret_ref: {name: cmp-test-001-app-env, key: API_KEY}}
```

성공 반환은 `environment`, `provider`, `namespace`, ID·종류를 유지한 `resources[]`, `validation`으로 구성한다. 각 DB 원소는 `release_name`, `helm_values`, `manifest_yaml`, `outputs={host,port,name,username,password_secret_ref,connection_url_secret_ref}`를, 각 앱 원소는 `release_name`, `helm_values`, `manifest_yaml`, `resolved_env_bindings`를 반환한다. 예를 들면 `DB_HOST`는 DB Service 주소, `DB_PASSWORD`와 `API_KEY`는 `{secret_name,secret_key}`가 된다. `connection_url_secret_ref`는 항상 `{name:<DB release>-url,key:url}`이며, 같은 이름의 비밀번호·사용자 Secret이 있으면 `SECRET_NAME_CONFLICT`로 거절한다. URL Secret 자체는 렌더가 만들지 않고 실행 주체(ON_PREM Agent, AWS/GCP 배포 Activity, Sandbox 워커)가 공통 함수 `cmp_providers.database_url.connection_url`로 만든다. `validation={passed:true,errors:[]}`는 **렌더 검증**일 뿐 실제 Apply·Pod Ready·DB 접속 성공이 아니다. 실패는 안전한 필드 경로·오류 코드로 반환한다.

현재 `APPLICATION`·`POSTGRESQL`만 렌더하고 나머지 종류 및 PostgreSQL 확장은 거절한다. 여러 앱·DB를 지원하며 앱 환경변수마다 해당 DB의 ID·출력 또는 이미 준비한 Secret 이름·키를 요구한다. DB 버전 필드는 16으로 제한하지만 **이미지 Digest 자체의 실제 PostgreSQL 버전·확장·대상 Pull 가능성은 조회하지 않는다.** Worker가 Planning 요구와 이미지 출처, 대상 capabilities·실제 Secret/StorageClass를 대조해야 한다. Profile은 요구를 덮어쓰지 못한다. `network_intent`는 Sandbox와 운영 Worker가 각 환경의 정책으로 해석하고 Sandbox 전용 원본 클라우드 차단 규칙은 별도로 적용한다. v1의 `release_names.app/db`, `app`, `db`, `env_bindings` 최상위 필드를 v2 입력으로 그대로 전달할 수 없다.

DB Values는 기존 `PostgresChartValues` v1을 리소스마다 재사용하고, 앱은 같은 Chart의 `ApplicationValuesV2` 분기를 사용한다. 별도 수정 설정 산출물이 있을 때만 앱의 `config_ref`를 넣는다. 앱 Values의 환경변수는 리소스 출력에서 확정한 문자열 또는 Secret 이름·키 참조이며 raw `.env` 값은 허용하지 않는다. Chart는 외부 Helm CLI로 **template**만 실행한다. Manifest·Values의 저장/Digest·승인 산출물 대조, 클러스터 적용·검사·정리, 실제 네트워크 정책은 호출 Worker가 맡는다.

```python
from pathlib import Path
from cmp_providers.shared_deployment_v2 import render_shared_deployment_v2

result = render_shared_deployment_v2(
    payload,
    database_chart_dir=Path("infra/helm/postgres"),
    application_chart_dir=Path("infra/helm/application"),
)
```

## Agent 등록과 소유

P0는 공개 회원가입 대신 플랫폼의 최소 소유자 식별자에 Agent를 연결한다. 등록 권한은 소유자에 묶인 일회용·만료 토큰으로 위임하며, 등록된 Agent는 아웃바운드 연결로 자신에게 할당된 작업만 조회·보고한다. 등록 토큰과 Agent 인증 원문을 업무 DB·Temporal History·로그에 남기지 않는다. 운영 배포 작업은 특정 승인 Plan ID·Revision·Digest와 Agent·클러스터에 고정되어야 하며 07·12에서 실행 연결을 한다.

Agent가 접속하는 대상 K3s는 사용자 측에 있고 Sandbox 클러스터는 플랫폼이 미리 준비한 별도 자원이다. 공통 모듈이 플랫폼 서버 내부에 앱/DB를 띄우는 것은 아니다.

04A에서는 플랫폼 운영자 키를 가진 호출만 `POST /agent-enrollments`로 토큰을 발급한다. Agent는 `POST /agents/register`로 토큰을 한 번 사용해 인증 값을 받고, `POST /agents/{agent_id}/jobs/claim`과 결과 보고 API로 자기 작업만 다룬다. DB에는 토큰·인증 값의 SHA-256 Digest만 저장한다. 전송 가능한 작업 본문은 `SANDBOX_TEST`의 버전 참조, 12의 `PRODUCTION_ROLLOUT`(아래), MNP-01 N3의 `NETWORK_PREFLIGHT`(검사 요청·Secret 이름/키 참조만, 결과는 `ProbeObservation`)이다. `remote_agent` CLI의 `poll-once`는 참조 조회만 하고, 승인된 운영 배포는 `python -m cmp_providers.onprem_agent`(1회) 또는 장기 실행 `python -m cmp_providers.agent_runtime`(15)이 실행한다.

### 상시 실행·설치·폐기 (15, #69)

- 발급 시 `cluster_uid`는 선택이다. 비우면 최초 등록 때 Agent가 보고한 kube-system UID로 고정되고, 지정했다면 기존처럼 일치해야 한다. 빈 UID 등록은 거절한다.
- 인증된 Agent 요청마다 `registered_agents.last_seen_at`을 갱신한다. 운영자는 관리자 키로 `GET /agent-admin/agents/{agent_id}`(상태)와 `POST /agent-admin/agents/{agent_id}/revoke`(폐기)를 호출한다. 폐기(`revoked_at`)된 Agent는 claim·결과 보고·산출물 다운로드·자기 상태 조회가 모두 403이고, Agent ID는 재사용하지 않으며 남은 작업은 지우지 않는다. 결과 보고의 인증 실패는 이제 409가 아니라 403이다.
- 장기 실행 Agent(`cmp_providers.agent_runtime`)는 등록 → 폴링 루프를 하나로 합치고 사용자 K3s 안에서 ServiceAccount로 실행한다(`ClusterTarget.in_cluster`). 설치·운영 절차는 [onprem-agent-install.md](onprem-agent-install.md)를 따른다.

### 터널 설정 교환 (16, #70)

Agent는 기존 Bearer 자격 증명으로 WireGuard **공개키만** 등록한다(`PUT /agents/{agent_id}/tunnel/key`). 응답과 `GET /agents/{agent_id}/tunnel/config`에 배정 터널 IP(`10.200.0.0/24`의 /32, 중계 서버는 `.1`), 중계 서버 목록(`endpoint`·`public_key`), 설정 버전이 있고, 설정 조회에는 MTU 1420, PersistentKeepalive 25, 노출 포트(5432 + READY 앱 포트)도 있다. Agent는 터널 경유 앱 헬스·DB 접속 결과와 실패 원인 코드(`code`, 성공이면 `null`, 17 W8)를 `POST /agents/{agent_id}/tunnel/status`로 보고한다. Agent 쪽 WireGuard 클라이언트·터널 IP 전용 전달기는 [17 티켓](../../tickets/provisioning/17-agent-wireguard-tunnel.md)과 [Agent 설치 문서 §터널](onprem-agent-install.md#7-wireguard-터널-17)을 따른다. 중계 서버는 `GET /relays/{relay_id}/agents`를 폴링해 peer를 반영하고, 폐기 Agent(`revoked: true`)는 peer를 지운다. 확인이 끝난 ON_PREM 결과만 Data(`tunnel_endpoint = 터널IP:5432`)·Traffic(`external_host = 터널IP`, `external_port = 앱포트`) 인계가 진행된다. 상세는 [16 티켓](../../tickets/provisioning/16-tunnel-config-exchange.md)을 따른다.

### DB 연결 URL Secret (19, #79)

앱이 `connection_url_secret_ref`를 참조하면 작업 본문 `database.connection_url`에 대상 Secret 참조와 DB 이름·사용자(비밀이 아닌 값)가 들어간다. 이 참조는 사용자가 미리 만들어야 하는 Secret 목록(`secret_refs`)에서 빠진다. Agent는 변경 전에 같은 이름의 Secret이 `cmp.dev/managed=true` 없이 있으면 `SECRET_NAME_CONFLICT`로 멈추고, 비밀번호 Secret을 준비한 뒤 그 값으로 URL을 조립해 매 배포마다 Secret을 갱신한다. READY 검사는 URL Secret 키 존재를 확인한다. URL·비밀번호는 클러스터 밖으로 나가지 않는다. 바인딩이 없으면 필드를 생략해 기존 작업 본문과 같은 바이트를 유지한다.

## 승인된 운영 배포 (12, #36)

[12 착수 결정 E1~E9](../../tickets/provisioning/12-k3s-approved-rollout.md#착수-결정-2026-11-04)을 구현했다.

- **실행 연결**: 승인 실행 dispatch(claim → provider Activity → complete)는 08 소유다. 12는 `PROVISIONING_QUEUE`의 `execute_on_prem_plan`(`cmp_worker.activities.onprem_execution`)만 제공한다. 입력은 claim 결과(`approval_id`, `plan_id`, `revision`, `provider`, `scope`, `manifest_digest`), 출력은 `{status: SUCCEEDED|FAILED|UNKNOWN, code, resource_refs}`다.
- **작업 생성** (`cmp_api.onprem_rollout`): 업무 DB에서 APPROVE 기록·최신 Revision·manifest Digest를 다시 확인하고, Plan의 Chart/Values ArtifactRef 바이트를 `ArtifactStore`에서 읽어 Digest를 검증한다. 04B v2 렌더의 `PostgresChartValues`/`ApplicationValuesV2`에서 Release·StorageClass·Port·Health·Secret 이름만 꺼내 `cmp_contracts.onprem_rollout.ProductionRolloutJob`을 만든다. Plan의 `agent_id`·`cluster_ref.id`는 등록 Agent와 그 kube-system UID에 일치해야 한다. `job_id`는 `plan_id:revision`에서 결정되어 재시도해도 작업은 하나다(같은 ID·다른 본문은 `AGENT_JOB_CONFLICT`).
- **Agent 전달**: claim과 산출물 다운로드 시점에 승인을 다시 확인한다. QUEUED 작업이 승인 불일치면 Agent에 주지 않고 `FAILED`, RUNNING 작업이면 `UNKNOWN`으로 닫는다. Agent는 `GET /agents/{agent_id}/jobs/{job_id}/artifacts/{sha256}`로 자기 작업 참조만 받고 크기·Digest를 검증한다. P0 저장소는 Digest 파일명 로컬 디렉터리(`ARTIFACT_STORE_DIR`)이며 S3 연결은 후속이다.
- **Agent 실행** (`cmp_providers.onprem_agent`): 전용 kubeconfig·명시 context, kube-system UID 확인 → StorageClass → Namespace 소유(`cmp.dev/environment-id` 주석) → 사용자 Secret 존재 → DB 비밀번호 Secret 생성(클러스터 내부) → DB·App 순서의 `helm upgrade --install --wait`. Release 설명에 `job_id`와 Chart/Values Digest 표식을 남겨, 재수신 시 같은 표식의 `deployed` Release는 재설치하지 않고 검증만 한다. `pending-*`/`failed` Release는 `UNKNOWN INSPECTION_REQUIRED`로 남기고 자동 재적용하지 않는다. 변경 전 클러스터 접속 불가는 보고하지 않고 RUNNING을 유지한다.
- **오프라인·재연결**: Activity는 heartbeat하며 업무 DB 작업 상태를 조회하고, 대기 한도(기본 30분) 초과 시 작업을 취소하지 않고 `UNKNOWN AGENT_OFFLINE`(QUEUED)/`AGENT_RESULT_PENDING`(RUNNING)을 반환한다. 같은 요청으로 다시 실행하면 기존 작업을 재사용해 Agent 결과를 반영한다. 상태는 승인자 인증 `GET /provisioning/plans/{plan_id}/revisions/{revision}/rollout`으로 조회한다.
- **READY**: Release `deployed`, PVC `Bound`(지정 StorageClass), StatefulSet/Deployment ready·updated, 두 Service의 Ready Endpoint, DB Pod `pg_isready`, Service 프록시 경유 앱 health_path 2xx가 모두 통과해야 `SUCCEEDED READY`다. 하나라도 실패하면 해당 검사 코드로 `FAILED`다. `resource_refs`는 Namespace·Release·내부 Endpoint·DB Secret 이름/키·job_id이며 Secret 값은 없다. 외부 접근 경로·기능/데이터 검증은 13·Migration 범위다.

재현은 `scripts/smoke_onprem_rollout.py`(격리 k3d 전용, 전용 kubeconfig·`k3d-cmp-*` context 필수)이며 [실제 결과](../../docs/verification/provisioning-12-k3s.json)와 미검증 항목은 [12 티켓](../../tickets/provisioning/12-k3s-approved-rollout.md#검증-기록-2026-11-04)에 있다.
