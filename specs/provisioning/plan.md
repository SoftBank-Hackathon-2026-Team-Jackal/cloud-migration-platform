# Implementation plan: Provisioning

## 1. 목적·근거·설계 상태

이 문서는 [Provisioning 요구사항](spec.md)의 `PROV-001`~`PROV-021`을 Python 3.11+, Pydantic v2, Terraform, Helm, Temporal로 구현하기 위한 **설계 제안**이다. 요구사항의 범위와 수용 기준은 `spec.md`, 제품 정책은 [policies.md](../../docs/product/policies.md), 모듈 책임은 [domain-boundaries.md](../../docs/architecture/domain-boundaries.md), 상위 실행 순서는 [temporal-flow.md](../../docs/architecture/temporal-flow.md)가 우선한다. 여기서 제안한 필드명·파일명·함수명·실행 분할은 팀이 채택하기 전까지 확정 계약이 아니다.

확정 전제는 AWS EKS 원본, AWS EKS·GCP GKE·온프레미스 K3s 대상, 세 대상 모두 Kubernetes의 PostgreSQL 16 StatefulSet/PVC, 별도 `.env` 업로드, Sandbox와 Production Worker 분리, Preview와 승인 후 실제 생성, P0에서 사용자 객체 스토리지 자동 생성·이전 제외다. S3는 플랫폼 산출물 저장소로 사용한다. 이전 EC2/Compute Engine + Docker Compose 경로는 이 설계의 대상이 아니다. Route 53·Envoy·WireGuard의 최종 방식은 아직 결정되지 않았고 트래픽 변경은 이 기능의 책임이 아니다.

## P0 후속 결정 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)가 아래 초기 제안의 해당 항목을 구체화한다. Preview/승인 유지, 핵심 산출물만 참조/Digest 고정, DB/App Chart 분리, AWS/GCP 기존 네트워크 재사용 제외, 중앙 S3 State+잠금, DB 승인 기록 유지·범용 Outbox 보류를 적용한다. 후속 사용자 지시로 한 티켓씩 순차 구현한다. 최소 공유 코드 CP-01의 실제 인터페이스는 [공유 계약](shared-contracts.md)을 따른다. 문서 수정은 구현 이슈/PR에 포함한다.

## 티켓 01 구현 현황

#5에서 내부 v1 계약과 순수 Planning/Provider 검증을 구현했다. 실제 위치는 `packages/contracts/src/cmp_contracts/provisioning.py`, `apps/worker/src/cmp_worker/provisioning.py`, `packages/providers/src/cmp_providers/provisioning.py`다. 작은 초기 범위라 기존 패키지의 단일 모듈로 두었고 아래 하위 패키지 분할은 후속 확장 제안으로 유지한다. 구현된 필드·미지원 범위·실행 증거는 [입력 계약 §10](input-contract.md)에 기록한다. 실제 상위 Worker의 wire 계약·S3 조회·Temporal 연결은 이번 구현에 포함되지 않는다.

후속 #1은 [입력 계약 v2](input-contract-v2.md)를 별도 공개 경계로 구현한다. Analysis의 리소스 목록, APPLICATION별 Build→Sandbox 출처·Digest·플랫폼, 대상·환경 참조를 검증하여 **Plan 발급 전의 Spec**을 반환한다. #23은 v2 다중 리소스 공통 Helm 렌더러를 별도 공개 경계로 구현한다. Planning Spec에서 대상별 구체 렌더 입력으로 변환하는 Worker, 실제 참조 조회와 최상위 Temporal 연결은 후속 구현이다.

#23은 [공통 배포 계약 v2](onprem-shared-deployment.md#v2-공통-렌더-입력반환)의 여러 APPLICATION·POSTGRESQL을 ID로 연결해 별도 Helm Values·Manifest로 렌더한다. 호출 Worker가 v2 Planning 요구와 대상별 이미지·Namespace·StorageClass·Secret 참조를 대조하고 최종 입력을 공급해야 한다. 이 렌더 결과를 Plan 저장이나 클러스터 적용 성공으로 간주하지 않는다.

## 2. Affected modules and contracts — 저장소에 맞춘 제안 구조

현재 저장소는 `backend/`와 `infrastructure/`가 아니라 아래의 `apps/`, `packages/`, `infra/`를 사용한다. 다음은 **추가·확장 제안**이며 이번 단계에서 실제 파일을 생성하지 않는다. 새 Python 서브패키지를 추가할 때에는 현행 `pyproject.toml`의 수동 `setuptools.packages` 목록과 package-dir 설정도 갱신해야 한다.

```text
packages/contracts/src/cmp_contracts/
  provisioning.py              # Pydantic v2 Spec·Preview·Result·Error 계약
packages/providers/src/cmp_providers/
  schemas/                     # Provider 중립 구성과 렌더링 결과
  adapters/aws.py              # EKS 대상 변환
  adapters/gcp.py              # GKE 대상 변환
  adapters/on_prem.py          # K3s 대상 변환
  rendering/                   # Terraform 입력, Helm Values·Manifest 렌더링
  bindings/                   # 일반 변수·동적 변수·Secret 참조 매핑
  validation/                 # CIDR·자원·Manifest 공통 검사
apps/worker/src/cmp_worker/
  provisioning/planning/      # 입력 조합·Plan/Preview 생성·산출물 기록
  provisioning/execution/     # Terraform, Helm, Agent, 자격 증명 접근
  provisioning/verification/  # 인프라·워크로드·앱 실제 검증
  provisioning/results/       # 실행 상태·결과 조립·저장
  activities/provisioning.py  # 위 서비스에 대한 Temporal Activity 경계
  workers/provisioning.py     # Provisioning Activity 등록
  workflows/migration.py     # 상위 단계·승인·진행 조건
apps/api/src/cmp_api/         # 기존 승인·조회 경계, 계획 기록 연계
infra/terraform/aws/         # VPC·EKS·애드온 등 대상별 구성
infra/terraform/gcp/         # VPC Network·GKE 등 대상별 구성
infra/helm/                  # 공통 App·PostgreSQL Chart, 대상별 Values
tests/                       # 계약·렌더링·승인·복구·실환경 검증
```

`cmp_contracts`는 Worker·API·후속 Migration이 공유할 직렬화 계약만 보유한다. `cmp_providers`는 순수 변환·렌더링·검증을 담당하며 클라우드 자격 증명, Terraform Apply, Kubernetes 변경, Agent 호출, Temporal 상태를 갖지 않는다. 실제 I/O, 자원 생명주기, Secret 조회, 산출물 저장은 Worker 서비스에 둔다. Sandbox 팀은 `cmp_providers`와 공통 검증 코드를 독립적으로 import하고 임시 Namespace 관리는 자체 Worker에서 수행한다. 기능 테스트 역시 실행기와 검사 정의를 분리해 두 환경에서 대상 주소·인증만 바꾸어 재사용한다.

04A(#20)는 [Sandbox/온프레미스 공통 배포 계약](onprem-shared-deployment.md)을 기존 DB/App Chart 모델과 바인딩 함수 위에 구현한다. 온프레미스 Agent 등록은 최소 소유자 기록·일회용 등록 토큰·Agent별 인증/작업 조회로 한정한다. 공개 회원가입은 만들지 않는다. 운영 Apply/Ready와 승인 연결은 07·12의 선행 조건을 따른다.

## 3. Design and data flow — 계획과 실행의 경계

### 앞 단계에서 `ProvisioningSpec`까지

수신 정보의 상세 기준은 [Provision 입력 계약](input-contract.md)이다. #5에서 선행 결과 입력, 조합된 Spec, 승인 후 실행 입력을 구분했다. 작은 메타데이터는 Temporal Activity 결과로 전달하고 큰 산출물은 S3의 불변 참조로 전달한다. 저장소 조회와 무결성·접근 검사는 Activity에서 수행한다. `artifact://`는 실제 프로토콜이나 Temporal 제공 기능이 아니다.

현재 확정된 것은 **정보의 출처와 논리적 순서**다. API는 원본 AWS 접근·소스·별도 `.env` 및 대상 선택의 참조를 받는다. Analysis는 원본 EKS·앱 요구사항과 변환된 코드·설정 참조를, Build는 준비된 소스·Dockerfile로 이미지 URI·Digest·플랫폼을 만들고, Sandbox는 그 Build 참조·Digest를 실행·검증한 결과와 코드/설정 출처를 낸다. 코드 재수정 시 새 Build·재검증하며 통과 후 재빌드하지 않는다. Provisioning Planning이 이 결과와 사용자 선택을 조합해 공통 `ProvisioningSpec`을 만드는 책임을 갖는 것으로 **제안**한다. `cmp_worker.provisioning.planning`이 그 조합 로직을 담는 위치 제안이다. 원본 파일·`.env`·Credential 원문 대신 참조를 전달하고, 큰 산출물은 S3 참조로 다룬다.

| 출처 | Planning이 소비할 정보 | 아직 정해지지 않은 계약 |
| --- | --- | --- |
| API·업무 DB | 대상 Provider/Profile, 원본·대상 접근 참조, 소스·`.env` 참조, 사용자 선택 | 생성 요청 필드·ID와 버전 |
| Analysis | 공통 앱·DB·네트워크 요구, 변환 코드·설정 참조 | 분석 출력 모델·자동/사용자 입력 우선순위 |
| Sandbox | 검증 상태·보고서, 실행한 Build 참조·Digest·플랫폼·설정 범위, 정리 결과 | 통과 기준·재검증/정리 실패 정책·실제 출력 모델 |
| Build | 성공 상태, 빌드 소스·Dockerfile/설정 참조·Digest, 이미지 URI·Digest·CPU 아키텍처 | 소스 Digest 산정 범위·실제 증거 형식 |
| Planning | 위 정보를 검증·조합한 Spec, 입력 산출물 참조·버전 | 구체적 Pydantic 필드, 누락/충돌 처리, Workflow 전달 방식 |

따라서 아래 흐름은 구현 입력 계약이 완성됐다는 뜻이 아니다. **티켓 01은 Adapter 로직을 확장하기 전에** 이 선행 출력의 최소 계약과 Spec 조합 인터페이스를 확정·검증해야 한다. 상위 단계의 구체적 출력 모델과 Workflow/Activity 전달 방식은 담당 영역과 합의가 필요하며, 미정 상태에서 임의의 JSON 필드를 사실상 표준으로 만들지 않는다.

```text
Analysis·Build·Sandbox 결과 + 사용자 대상 선택 + 업로드 .env 참조
  → ProvisioningSpec 구성·검증 → Provider Adapter
  → Terraform 입력·Helm Values·Manifest 렌더링 → 정적 검증
  → 클라우드 대상 조회 + Terraform init/validate/저장 Plan (AWS/GCP)
    또는 Agent의 읽기 전용 대상 점검·렌더 검증 (ON_PREM)
  → 민감정보 제거된 Preview + 불변 산출물 참조 저장
  → Plan ID·Revision 승인 대기
  → 승인 기록·산출물 Digest 재검증
  → AWS/GCP: 승인된 Terraform Plan Apply → 클러스터 접속 준비
    ON_PREM: 승인된 작업을 Agent에 전달 → 기존 K3s 점검
  → 대상별 선행 구성요소 확인 → Secret 생성·주입
  → Helm App·PostgreSQL 배포 → 실제 환경 검증
  → ProvisioningResult 저장 → Data Migration / Traffic Migration 전달
```

클러스터·네트워크 준비와 Kubernetes 워크로드 배포는 다른 실행 단위다. 전자의 성공만으로 Provisioning `READY`를 반환하지 않는다. Preview의 조회·검증은 대상 생성 권한 없이 수행할 수 있어야 하고, `terraform plan`/`helm template`/Agent 점검은 대상 자원 변경 없이 실행한다. Backend 초기화나 Provider 조회에 필요한 계정·저장소는 사전 준비하고 Preview가 이를 새로 만들지 않게 한다. ON_PREM에서 읽기 전용 점검을 지원하지 않는 Agent라면 Preview에는 검증 불가 항목을 명시하고 승인 전 배포 작업은 전송하지 않는다.

## 4. Pydantic v2 계약과 Provider별 검증 (`PROV-001`~`004`, `019`, `021`)

아래는 `cmp_contracts.provisioning`에 둘 **제안 모델**이다. 단위·Enum·정확한 필드명·기본값은 구현 시 결정하고 계약 버전으로 고정한다. 모든 모델은 `extra='forbid'`와 명시적 스키마 버전 사용을 권장한다. Secret 값 자체는 모델에 넣지 않는다.

| 제안 모델 | 핵심 필드와 제약 |
| --- | --- |
| `PlanRef` | `plan_id`, 양의 `revision`, `template_version`, `contract_version` |
| `ImageSpec` | Digest가 고정된 URI, 지원 CPU 아키텍처 목록, Registry 인증 참조 |
| `PodResources` | CPU·메모리 요청/제한. Node 사양과 별도 |
| `ApplicationSpec` | `ImageSpec`, `PodResources`, replicas, container port, health path |
| `NodeRequirement` | Node 수 범위, 유형 지정 또는 Adapter 선정 정책, Node 디스크·아키텍처 요구 |
| `DatabaseSpec` | PostgreSQL 버전 16, DB 이름·사용자, `PodResources`, PVC 용량, 인증 생성 정책 |
| `EnvironmentSpec` | `env_file_ref`, 변수 재매핑 규칙, 비밀값 식별 메타데이터. 원문 제외 |
| `NetworkRequirements` | P0 신규 네트워크 요구(기존 사용은 미지원), CIDR 제약, subnet 목적, 아웃바운드·보안 통신 요구 |
| `IngressRequirements` | 서비스/대상 포트, 외부 접근 방식, DNS·TLS 요구 또는 참조 |
| `AwsTarget` / `GcpTarget` | `provider` 리터럴, credential ref, region, 클러스터·Node 요구, 네트워크 요구, 대상별 선택값 |
| `OnPremTarget` | `provider='ON_PREM'`, Agent ID, 클러스터 참조, Namespace, StorageClass, 용량 요구 |
| `ProvisioningSpec` | `PlanRef`, 판별 가능한 `target`, 앱·DB·환경·Ingress, 이전 연결 요구 |

**권장:** Pydantic v2 `Field(discriminator='provider')`로 `AwsTarget | GcpTarget | OnPremTarget`을 구분한다. 이렇게 하면 클라우드 자격 증명·Region·Node 요구와 ON_PREM의 Agent·기존 K3s 요구가 타입 수준에서 달라진다. `model_validator(mode='after')`는 `CREATE`일 때 CIDR 필요, `EXISTING` 요청은 P0 미지원 오류, TLS 요청 시 도메인·인증 정책 필요, 이미지 아키텍처와 Node 선정 정책의 호환성처럼 **교차 필드**에 사용한다. 단일 모델의 광범위한 Optional 필드와 Validator만으로 Provider별 필수값을 표현하는 방식은 오류가 늦고 계약이 불명확해 권장하지 않는다. Pydantic의 정적 검사 뒤에 Adapter의 대상별 지원성·권한·실제 CIDR 충돌·할당 가능성 검사를 별도로 둔다.

Node 수·유형의 `EXPLICIT`/`POLICY_SELECTED` 같은 선정 모드를 제안한다. Explicit는 승인 입력을 그대로 사용하고, 선정 모드는 Profile·자원 요구·Provider 제한으로 후보를 계산한 뒤 **선정된 실제 Node 유형·수·비용 관련 변경을 Preview에 표시**한다. Pod CPU·메모리와 PostgreSQL PVC는 Node 디스크 크기와 서로 다른 값이다. 지원 아키텍처와 Node 아키텍처가 맞지 않으면 계획을 거절한다. 자동 선정 알고리즘과 자원 기본값은 미확정이다.

네트워크 명세는 `앱 외부 443`, `앱→DB 5432`, `클러스터 아웃바운드`, `DB 비공개` 같은 목적·허용 경로를 주로 표현한다. Adapter가 AWS VPC/Public·Private Subnet/Route Table/Internet Gateway/NAT·Endpoint/Security Group, GCP VPC Network/Regional Subnet/Cloud Router·NAT/Firewall에 각각 매핑한다. CIDR 형식·중복·포함 관계, 기존 네트워크와 충돌, 필수 인터넷·Registry·Kubernetes API·DB 경로, 과도한 DB 공개 여부를 정적 및 대상 조회 검사로 확인한다. 실제 서브넷·Route 규칙은 Provider별 결과에 남긴다.

## 5. 공통 Provider 인터페이스와 산출물 (`PROV-002`, `005`, `006`)

다음은 함수 시그니처 **제안**이다. `ValidationReport`는 `code`, `path`, `message`, 심각도와 승인 가능 여부를 포함하고, 민감정보를 메시지에 포함하지 않는다. 함수는 외부 I/O를 하지 않으며 실패 시 구조화된 검증 오류를 반환하거나 타입화된 도메인 오류를 일으킨다.

```python
validate_spec(spec: ProvisioningSpec) -> ValidationReport
adapt(spec: ProvisioningSpec, policy: TemplatePolicy) -> ProviderConfiguration
build_bindings(env_keys: list[str], mapping: MappingRules,
               target_refs: TargetReferenceSet) -> BindingPlan
render_terraform(config: ProviderConfiguration) -> TerraformInputs | None
render_helm_values(config: ProviderConfiguration,
                   bindings: BindingPlan) -> HelmValues
render_manifests(values: HelmValues, template_version: str) -> RenderedArtifacts
validate_rendered(artifacts: RenderedArtifacts) -> ValidationReport
```

`ProviderConfiguration`은 Provider별 네트워크·클러스터 계획과 공통 앱·DB·Ingress 설정을 구분한다. `RenderedArtifacts`는 Manifest·Values·Terraform 입력의 **산출물 참조**, 예상 리소스 목록, 템플릿 버전, 적용된 기본값, 검증 결과를 담는다. 실제 업로드와 참조 발급은 Planning 서비스가 담당한다. 큰 내용은 S3에 버전·Digest와 함께 보관하고 Workflow History에는 참조·작은 요약만 전달한다. 저장 실패는 Preview 게시 실패로 처리한다. 템플릿은 승인 전후 동일 버전·Digest를 사용하며 임의 외부 Helm 의존성은 잠금 버전으로 고정한다.

Sandbox는 동일한 `build_bindings`·`render_helm_values`·`render_manifests`·`validate_rendered`를 사용하되 Sandbox Values, 임시 DB·Secret 참조, 자체 배포·정리 서비스를 쓴다. Production은 대상별 Values, 실제 DB·Secret 참조와 별도 실행 서비스를 사용한다. 공통 검사에는 Manifest 구조·Service 포트·ConfigMap/Secret 참조와 테스트 정의를 두지만 대상별 IAM, CSI, Load Balancer와 실제 연결 검증은 Production에 남긴다. Sandbox 성공을 Production 검증으로 대체하지 않는다.

## 6. `.env`와 Secret의 처리 시점 (`PROV-004`, `012`, `021`)

업로드 단계가 제한된 저장소에 `.env` 원문을 보관하고 `env_file_ref`를 발급한다. Analysis/Planning은 필요 범위에서 키·유형·재매핑을 검증하며 Preview에는 키 이름·대상 참조·마스킹된 값만 보인다. 네 범주를 분리한다: 일반 정적 값, 대상 Endpoint처럼 생성 후 결정되는 값, 사용자 제공 Secret, 새 PostgreSQL 인증처럼 Provisioner가 생성하는 Secret. `BindingPlan`에는 값 대신 범주·대상 키·참조와 생성 시점을 기록한다. 예를 들어 `DB_HOST`는 생성된 DB Service, `DB_PORT`는 Service port, `DB_NAME`은 승인된 DB 이름, `DB_PASSWORD`는 새 인증 Secret을 가리킨다. 원본 DB 비밀번호를 그대로 복사한다고 가정하지 않는다.

승인 후 실행 Activity가 필요한 시점에 Secret 저장소에서 원문을 읽고 새 DB 인증 정보를 안전하게 생성·저장한 뒤 Kubernetes Secret을 만들거나 참조한다. 일반 값은 ConfigMap, 민감값은 Secret으로 주입한다. 재시도에서는 기존 Secret 참조·버전을 먼저 조회해 비밀번호를 임의 재생성하지 않는다. Secret 생성 실패 시 DB·앱 배포를 진행하지 않는다. Secret 저장 제품과 회전 정책은 미확정이다. 인증값이 Process 로그, 예외, API 응답, Temporal 입력·결과·History, S3 일반 산출물에 들어가지 않도록 경계를 둔다. Terraform 변수·Plan·State에 Secret이 포함될 수 있음을 전제로, 가능한 한 앱/DB 비밀번호를 Terraform 입력에서 제외하고 Plan/State·작업 디렉터리·디버그 출력은 접근 제한·암호화·수명 관리한다. `sensitive` 표시만으로 State 비밀값이 제거된다고 가정하지 않는다.

## 7. Provider별 인프라·Agent 설계 (`PROV-007`~`009`)

| 단계 | AWS EKS | GCP GKE | ON_PREM K3s |
| --- | --- | --- | --- |
| 인프라 구성 | Terraform 네트워크(VPC·Subnet·Route·IGW·필요 시 NAT/Endpoint·SG) → EKS·Managed Node Group → 접근 및 애드온 | Terraform 네트워크(VPC Network·Subnet·필요 시 Cloud Router/NAT·Firewall) → GKE·Node Pool → 접근 및 스토리지 준비 | 기존 K3s·Agent 연결·Namespace·StorageClass 확인. 클러스터 신규 생성은 범위 밖 |
| 배포 접근 | 클러스터 Endpoint·인증 정보를 실행 시 조회, Kubernetes RBAC 확인 | 클러스터 Endpoint·인증 정보를 실행 시 조회, Kubernetes 권한 확인 | Agent가 플랫폼에 먼저 연결하거나 작업을 가져와 로컬 Kubernetes 권한으로 실행 |
| 외부 접근 | AWS Load Balancer Controller의 권한·설치·준비를 확인한 뒤 ALB Ingress | GKE에 맞는 Ingress/LB 구성. AWS Controller 재사용 금지 | Agent가 내부 Service·Ingress 정보를 반환. 외부 중계·터널 생명주기는 별도 영역 |
| 영속 스토리지 | EBS CSI Driver·StorageClass와 PVC Bound 확인 | GKE Persistent Disk 계열 CSI·StorageClass와 PVC Bound 확인 | 제공된 StorageClass와 PVC Bound·실제 마운트 확인 |

Terraform 모듈 경계 **제안**은 AWS의 `network`, `eks`, `access`, `addons-prerequisites`와 GCP의 `network`, `gke`, `access`, `storage-prerequisites`다. 정확한 디렉터리 분할·기존 VPC 참조·애드온의 Terraform/Helm 소유권은 후속 선택이다. 공통 실행 순서는 네트워크·권한 → 클러스터/Node → Kubernetes API 접근 → Controller/CSI·StorageClass 준비 → Helm DB·앱 → 외부 주소 검증이다. ALB 자체는 Ingress Controller의 비동기 조정 결과일 수 있으므로 Terraform Output만으로 생성 완료를 판단하지 않고 Ingress 상태·주소·실제 응답을 확인한다. GKE도 Ingress/LB의 실제 상태를 별도로 조회한다. EKS IAM(클라우드 API 권한), Kubernetes RBAC(클러스터 리소스 권한), Pod의 AWS API 권한은 각각 별도 주체·정책으로 설계한다. GKE의 클라우드 권한과 Kubernetes 권한도 분리한다.

ON_PREM Agent에는 Plan ID·Revision·작업 ID·산출물 Digest·필요한 Manifest/Values 참조와 허용된 배포 동작만 전달한다. Agent가 플랫폼에 먼저 연결/폴링하는 방식을 **권장**한다. 플랫폼의 PC 직접 인바운드는 전제하지 않는다. 요청은 Agent ID·대상 클러스터·Namespace에 묶고 중복 작업 ID에 대해 기존 상태를 반환하도록 한다. 연결이 끊기면 실행 상태를 `UNKNOWN/WAITING_AGENT`로 보존하고 재연결 뒤 조회한다. 12(#36)에서 Agent는 폴링으로 작업을 받고 결과 보고 API로 완료를 알리며, Worker Activity가 heartbeat하며 업무 DB 작업 상태를 조회하는 방식으로 확정했다([공통 배포 계약 §승인된 운영 배포](onprem-shared-deployment.md#승인된-운영-배포-12-36)). K3s를 위한 macOS Linux VM 도구, 외부 Envoy·WireGuard는 이 설계에서 확정하지 않는다.

## 8. Terraform Plan·Apply·State (`PROV-014`~`017`, `021`)

05의 AWS 경로는 `infra/terraform/aws`의 고정 템플릿과 `cmp_providers.aws_preview`의 명시적 신규 VPC·Subnet·EKS·Node 입력을 사용한다. 운영 Plan의 S3 Backend는 기존 버킷의 환경별 key, 암호화, Terraform lockfile 잠금으로 초기화하며 Plan 시 AWS 계정을 재확인한다. 저장 binary Plan·Spec의 바이트 Digest, 템플릿·Provider lockfile Digest, Plan ID·Revision·대상/State 식별을 기록한다. Preview는 `terraform show -json`의 자원 종류·행위만 허용 목록으로 읽고 검증된 입력 사양을 표시한다. 05의 로컬 State 검증 Plan은 `LOCAL_STATE_VALIDATION_ONLY` 미해결 결정을 남겨 승인할 수 없으며, 실제 승인 전에 S3-backed 최종 Plan·Preview를 새로 생성해야 한다. 이 Preview는 `INFRASTRUCTURE` 범위이며 ALB/앱/DB 배포 완료나 전체 승인 가능 상태를 뜻하지 않는다. 영속 산출물 저장 연동은 후속 범위다.

클라우드 경로는 **구성 생성 → init → validate → 저장 Plan → Preview → 승인 → 저장된 Plan Apply → Output/실제 상태 조회** 순서다. Terraform이 관리하는 것은 네트워크·클러스터 등 인프라이고 Helm은 Kubernetes 워크로드를 관리한다. Chart/Values도 승인 계획에 묶어 두 실행 계층의 구성이 같이 고정되게 한다.

**Plan/Apply 동일성 권장안:** Plan ID·Revision에 `ProvisioningSpec` 정규화 Digest, 이미지 Digest, Terraform 구성/모듈·Provider lockfile Digest, Helm Chart/Values **템플릿과 동적 바인딩 규칙**의 Digest, Template Version, 대상·계정·Backend State 식별자와 저장된 Terraform binary Plan Digest를 기록한다. DB Endpoint·생성 Secret 참조처럼 Apply 후에만 결정되는 값은 승인된 바인딩 위치에만 채우고, 최종 Values의 다른 부분이 Preview의 잠긴 구성과 같은지 검증한다. 사용자용 Preview는 이 잠긴 산출물에서 파생한 요약이다. 승인 기록은 동일 Plan ID·Revision·Preview Digest를 참조한다. Apply 직전에 승인·현재 Revision·파일 Digest·대상 계정·State 식별자를 재확인한다. 변경이나 State drift로 저장 Plan이 무효하면 **재계획·새 Revision·재승인**으로 돌아가고 조용히 새 Plan을 Apply하지 않는다. Preview에는 생성/변경 자원·Node/네트워크·보안·앱/DB/PVC·Terraform 변경 요약·Helm 예상 리소스·오류/경고를 보여주되 Plan 원문·Secret은 노출하지 않는다.

**State 결정:** Migration/대상 환경별 독립 Backend key를 두고 공유 인프라와 충돌하지 않게 소유 경계를 기록한다. 원격 Backend, 암호화·최소 권한·버전 관리, State locking을 권장한다. 중앙 S3 Backend와 잠금을 채택한다. 구체 Terraform 버전·잠금 설정·버킷/권한은 공통 실행 설정으로 확정한다. P0에서는 기존 네트워크를 지원하지 않는다. 외부 소유 자원을 State로 가져오지 않는다. 동일 State에 대한 동시 Apply를 잠금 및 업무 DB 실행 상태로 방지하고, 잠금 획득 실패는 대상 변경 없이 재조회/대기한다. Worker 장애 후에는 lock 상태·State·실제 클라우드 자원을 확인한 다음 재개한다. 로컬 Plan·State 임시 파일은 보호된 작업 공간에 두고 로그·Git에 남기지 않는다.

Apply가 부분 실패하면 Terraform State와 대상 API를 조회해 생성·변경된 자원을 기록한다. 자동 재시도 가능성을 판단하되 원인 불명·공유 자원·데이터가 있는 경우 사람의 조치 상태로 둔다. 무조건 `destroy`하지 않는다. Terraform Output은 Cluster/Network ID 등만 `ProvisioningResult`에 매핑하며 Secret Output은 일반 결과로 보내지 않는다. State는 중앙 S3와 잠금을 사용한다. 실제 설정과 부분 실패 후 자동 정리 정책은 미정이다.

## 9. Helm과 Kubernetes 배포 (`PROV-010`~`013`, `016`~`018`)

`infra/helm/`에 DB와 App의 별도 Chart/Release를 둔다. DB Chart는 Sandbox/Production에서 공유하고 StorageClass 및 Secret 이름·키를 명시적으로 입력받는다. 공통 Values에는 이미지 Digest, Pod resources, replicas, ports, health, DB, 일반 설정과 Secret 참조를 넣고 Provider별 Values에는 StorageClass, Ingress/LB·Controller, 필요한 네트워크 설정을 둔다. Sandbox는 동일 Chart에 격리된 Namespace·테스트 Values를 적용한다. Chart는 환경별 Secret 원문을 Git에 넣지 않는다.

실행 순서는 Namespace/StorageClass·Controller 준비 → DB 인증 Secret 준비 → PostgreSQL Helm 설치/업그레이드 → PVC Bound·Pod Ready·DB 접속 확인 → 대상 DB Service와 비밀 참조로 앱 Values 완성 → App Helm 설치/업그레이드 → Pod Ready·Service·Ingress/LB 주소 확인이다. 데이터 이전용 복제 역할·설정 적용 가능 여부도 확인하되 실제 복제는 Data Migration에 맡긴다. Registry Pull 인증·CPU 아키텍처를 배포 전에 검증한다. NetworkPolicy는 대상 지원성과 필수 경로를 검토한 뒤 렌더한다. Pod Ready나 PVC 객체 존재만으로 DB 접근·실제 Volume 사용 가능성을 선언하지 않는다.

재시도 시 Release 이름·Namespace·Chart/Values Digest와 실제 Helm Release 상태를 조회한다. 성공 응답 유실이면 적용 상태를 결과로 복구하고, 실패/미완료이면 충돌·기존 Revision·Kubernetes 객체를 조사한다. 승인된 Values와 다른 내용으로 임의 업그레이드하지 않는다. Helm 자동 rollback·atomic 사용 여부는 PVC/DB 데이터 보존과 부분 배포 처리 정책이 정해진 뒤 선택한다.

**AWS 구현(09, #37):** `execute_aws_workloads` Activity가 위 순서를 EKS에 적용한다. 실행 전 업무 DB의 승인·최신 Revision·manifest Digest와 `PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/<sha256>`의 Chart tgz·Values·bindings 바이트를 재확인하고, 08 인프라 실행의 `resource_refs`(클러스터·VPC·LB Controller IRSA)로 대상을 찾는다. StorageClass(gp3 EBS CSI)·AWS Load Balancer Controller(고정 Chart 버전·SHA-256)를 준비하고 DB Secret은 Worker가 생성해 stdin으로만 전달한다. Helm은 기존 Release 조회 후 install/skip/upgrade하고 `pending-*`는 UNKNOWN, `failed`는 FAILED로 멈추며 atomic·rollback은 쓰지 않는다. Ingress는 앱 Chart 계약을 바꾸지 않고 승인 Values에서 만든 고정 ALB(HTTP:80, 도메인·TLS 없음) 템플릿이다. PVC Bound·인증 DB 접속(PostgreSQL 16)·Pod Ready·내부 헬스·ALB 주소·외부 헬스가 모두 통과해야 `WORKLOADS_READY`다. 결정 상세는 [09 티켓](../../tickets/provisioning/09-aws-workloads.md) W1~W11.

## 10. Temporal Activity, Preview, 승인 (`PROV-014`~`017`)

상위 Migration Workflow는 계획 → Preview → 특정 Plan ID·Revision 승인 대기 → 실행 → 검증 → 결과 전달을 조정한다. Workflow 코드에는 파일·네트워크·시계 조회, Secret 조회, Terraform·Helm 호출을 넣지 않는다. 다음은 **Activity 경계 후보**이며 이름·Task Queue·Timeout·Retry 수치는 확정하지 않는다.

| 경계 후보 | 독립 분리 이유 | 재시도 전 확인 |
| --- | --- | --- |
| 명세 구성·검증/Adapter·렌더링 | 순수 계산은 묶을 수 있음; 오류를 Plan 전에 반환 | 입력·템플릿 Digest |
| 대상 조회·Terraform Plan/Preview 저장 | 외부 읽기·산출물 저장, 승인 이전 경계 | 저장된 Plan·Preview Revision |
| 승인 기록 조회 | 업무 DB와 Workflow Signal의 순서 불일치 방지 | 승인 주체·Plan ID·Revision·Digest |
| Terraform Apply/Output | 장시간·부수효과·State 잠금 | State·실제 자원·기존 실행 표식 |
| Agent 작업 제출/상태 조회 | NAT·오프라인·비동기 완료 | Agent 작업 ID·기존 실행 상태 |
| Secret 준비·Helm 배포 | Kubernetes 부수효과·Release 복구 | Secret 참조·Release Revision·객체 상태 |
| 실제 검증·결과 저장 | 외부 읽기·업무 기록/산출물 I/O | 검사 결과·저장된 Result 버전 |

승인 요청은 FastAPI가 권한과 Plan ID·Revision을 확인해 업무 DB에 기록하고 Temporal에 전달한다. Signal을 쓰는 경우 Signal 수락과 Workflow 재개를 구분한다. Workflow는 승인 이벤트만 믿지 않고 실행 직전 Activity로 DB 승인 기록과 잠긴 Plan Digest를 재검증한다. 거절·수정·오래된 Revision은 Apply로 가지 않는다. DB 기록 성공·Signal 실패와 반대 순서/중복에 대비해 승인 이벤트 ID와 재전달·조회 조정을 권장한다. P0에서는 범용 Outbox를 도입하지 않고 승인 이벤트 ID 기반 재요청/재전달과 상태 재조회로 전달 실패를 드러내고 복구한다. DB 승인 기록은 유지한다. 정확한 API URL·인증 연동·응답 계약은 07 착수 전에 확정한다.

장시간 Apply와 Agent 실행은 Heartbeat 또는 주기적 상태 조회를 두어 Worker 종료를 감지한다. 외부 프로세스가 계속 실행 중일 수 있으므로 Heartbeat timeout만 보고 같은 Apply/Agent 명령을 즉시 재발행하지 않는다. 실행 표식(`plan_id`, `revision`, `step`, 외부 작업 ID)과 State/Release/Agent 실제 상태를 먼저 조회한다. Activity Retry는 일시 오류에 한정하고 권한·검증·승인 불일치는 사용자/운영자 조치 상태로 보낸다. 취소 요청은 실행 중 외부 작업의 안전한 중단 가능성을 조회한 뒤 상태를 기록하며 인프라 자동 삭제로 해석하지 않는다. 이력에는 참조·상태·작은 오류만 남기고 Plan/Secret 원문을 싣지 않는다.

### 07 구현 (#34)

[07 착수 결정 D1~D6](../../tickets/provisioning/07-approval-gate.md#착수-결정-2026-11-04)을 구현했다. 업무 DB `provisioning_plan_revisions`(불변·증가 Revision, 최대값이 활성), `provisioning_approvers`(자격 증명 digest·허용 migration), `provisioning_approvals`(Revision당 결정 1건·전달 상태), `provisioning_executions`(`plan_id`·`revision`·`step` 고유 실행 표식)를 Alembic `20261104_01`로 만든다. API는 `cmp_api.routes.approvals`, 저장·재검증은 `cmp_api.approvals`, 전달은 Signal-With-Start(`cmp_api.approval_delivery`)다. `ProvisioningApprovalWorkflow`(`migration` queue)는 `approval_id`만 받고 `execute_approved_plan` Activity(`provisioning` queue)가 실행 직전 DB 승인·최신 Revision·`verify_approval_target`을 다시 확인한다. 기본 executor는 표식만 남기는 no-op이며 Terraform·Helm·Agent는 08/10/12에서 연결한다. 실행 표식이 이미 있으면(RUNNING/FAILED 포함) executor를 재호출하지 않으며, 실제 상태 조회 후 재개는 후속 실행 티켓 범위다.

### 08 실행 분기 (#2)

07의 단일 실행 Activity를 `claim_approved_execution`(DB 재검증·RUNNING 표식) → Provider Activity(장시간·Heartbeat·재시도 없음) → `complete_approved_execution`(SUCCEEDED/FAILED/UNKNOWN·참조 기록)으로 나눴다. 분기는 `cmp_contracts.execution.execution_steps`가 정한다: (AWS, INFRASTRUCTURE)→`execute_aws_plan`, (AWS, WORKLOADS)→`execute_aws_workloads`(09), (AWS, FULL)→둘 다 순서대로, ON_PREM→`execute_on_prem_plan`(12). Provider Activity가 사라지면(Worker 종료·Heartbeat 만료) Workflow는 `UNKNOWN`/`PROVIDER_ACTIVITY_LOST`를 기록하고 재실행하지 않는다. 상세는 [08 티켓](../../tickets/provisioning/08-aws-infra-apply.md)과 [AWS Terraform 안내](../../infra/terraform/aws/README.md)를 따른다.

### 10 GCP Apply (#2)

`execute_gcp_plan`(`cmp_providers.gcp_apply`)은 08 AWS 경로와 같은 보장을 GCP에 적용한다. 추가로 Plan 모드(`S3`만 허용), Google ADC 주체, 중앙 State를 가진 AWS 계정을 확인한다. 분기는 (GCP, INFRASTRUCTURE)→`execute_gcp_plan`, (GCP, WORKLOADS)→`execute_gcp_workloads`(11), FULL→둘 다 순서대로다. 템플릿은 `gcp-gke-v2`(Output, 삭제 보호 변수)다.

### 11 GCP 워크로드 (#43)

`execute_gcp_workloads`(`cmp_worker.activities.gcp_workloads`)는 09의 승인 워크로드 흐름을 공통 기반(`k8s_workloads`·`k8s_workloads_cli`)으로 재사용하고 GCP 고유 부분만 더한다: gcloud 설정 프로젝트·클러스터 `RUNNING` 확인, PD CSI StorageClass(`pd-balanced`·Retain), 승인된 ECR Digest를 위한 단기 Pull Secret(`cmp-ecr-pull`, default ServiceAccount 연결, 토큰은 stdin만), GCE Ingress(외부 HTTP LB, NEG)와 외부 헬스 200. AWS LB Controller는 쓰지 않는다. 대상 클러스터는 10 G4 refs(`gcp_project_id`·`gke_cluster_name`·`gke_location`)로 찾는다. 결정 K1~K12와 실검증은 [티켓 11](../../tickets/provisioning/11-gcp-workloads.md)에 있다.

### 12A 온프레미스 Preview (#41)

ON_PREM은 Terraform Plan이 없으므로 Preview를 승인 대상 Values 바이트에서 만든다(`cmp_providers.onprem_preview`). 18(#98)이 이 함수를 운영 Plan 게시 경로에서 호출한다. AWS/GCP와 같은 공통 `Preview`·승인 계약을 쓰며, 클러스터 조회 없이 Agent가 실행할 DB·앱 변경과 기본값을 보여 준다. 기존 Release 여부는 12의 Agent가 실행 시 확인한다.

## 11. ProvisioningResult와 후속 단계 (`PROV-018`~`020`)

`ProvisioningResult` 제안 필드는 `plan_id`, `revision`, `provider`, `status`, `region/namespace`, `cluster_ref`, `network_resource_refs`, `app_internal_endpoint`, `app_external_endpoint`, `app_port`, `health_path`, `db_host`, `db_port`, `db_name`, `db_user`, `db_secret_ref`, `helm_release_refs`, `plan/state/manifest/values_refs`, `verification`, `failure_stage`, `error_code`, `recoverability`다. 성공·부분 실패 모두 구조화된 결과를 저장하되 필수 항목이 빠진 부분 실패를 `READY`로 간주하지 않는다. 실패 시 아직 생성되지 않은 Endpoint는 비워 두고 완료된 자원만 기록한다. 비밀번호·`.env` 원문은 결과에 넣지 않는다.

검증은 세 층으로 기록한다. **Infrastructure:** 클러스터 접근, Node Ready, 네트워크 경로, Controller/CSI, StorageClass/PVC Bound, Ingress/LB 준비. **Workload:** Deployment·StatefulSet·Pod Ready, 이미지 Pull, Service, ConfigMap/Secret 참조와 DB 접속. **Application:** 헬스·필요한 기능 테스트와 환경변수 매핑. 각 항목에 상태·대상 리소스·오류 코드·증거 참조를 둔다. 데이터 이전 전에는 DB 데이터가 없을 수 있으므로 인프라·서비스 준비용 검사와 데이터 이전 후 기능·정합성 검사를 별도 단계로 둔다. 필수 준비 검사 실패 시 `READY` 금지다. Sandbox 테스트 정의를 `target_endpoint`, 인증 참조, 테스트 데이터/정리 정책을 받는 실행 인터페이스로 재사용할 수 있으나 Production에서 데이터 변경 테스트를 자동 실행할지는 결정이 필요하다.

Data Migration에는 대상 DB Endpoint·네트워크 접근 요구·인증 참조·DB 준비 상태를, Traffic Migration에는 앱 Endpoint·Ingress/LB 주소·헬스 경로·전환 준비 상태를 넘긴다. 대상 DB가 준비됐다는 사실이 데이터 복사·CDC 완료를 의미하지 않는다. 외부 라우팅 변경·Envoy 가중치·DNS Cutover는 Traffic Migration에서 수행한다. Workflow는 Provisioning 결과를 참조해 다음 단계를 시작하지만 전체 Migration `COMPLETED`로 표시하지 않는다.

**13 구현 (#53):** 위 제안 필드를 `ProvisioningResult` v1로 확정했다. 승인 실행 완료 시 업무 DB `provisioning_results`에 불변 저장하고, Workflow 이력에는 `plan_id`·`revision`·`status`만 남긴다. Data/Traffic Worker는 `get_migration_handoff` Activity로 자기 섹션과 진행 가능 여부를 읽고, 사용자는 읽기 전용 요약 API로 상태·앱 외부 주소·검사 결과만 본다. 검사 목록은 Provider별 고정 검사 순서와 첫 실패 코드에서 추론하며, 데이터 변경 기능 테스트는 항상 `NOT_RUN`(데이터 이전 후 실행)이다. 상세 결정은 [티켓 13](../../tickets/provisioning/13-result-handoff.md)에 있다.

## 12. Failure, retry, and rollback behavior

| 실패 | 처리·복구 경계 |
| --- | --- |
| 입력·CIDR·지원 불가 요구 | Plan 생성 전 구조화 오류. 자동 Apply 없음 |
| Credential 누락·권한 부족 | 필요한 권한만 진단해 사용자 조치 요청. 원문 출력 금지 |
| Terraform Plan 실패 | Preview 승인 불가. Plan 산출물·오류 참조 보관 |
| 승인 Revision/Digest 불일치·State drift | 실행 차단, 새 Plan/Preview/승인 필요 |
| Terraform Apply 부분 실패·Worker 종료 | 잠금·State·실제 자원 조회 후 유지/재개/수동 조치 결정. 무조건 Destroy 금지 |
| EKS/GKE 생성 지연 | 실제 클러스터 상태 조회와 제한된 재시도·Timeout. 수치 미정 |
| Helm 설치/응답 유실 | Release·Kubernetes 상태 조회 후 같은 승인 Values로 재개 |
| Image Pull 오류 | Digest·Registry 인증·Node 아키텍처 진단, READY 금지 |
| PostgreSQL PVC Pending | StorageClass·CSI·용량·Pod 이벤트 확인, READY 금지 |
| Agent 연결 끊김 | 작업 ID·Agent 상태 유지, 재연결 후 조회. 중복 제출 억제 |
| 헬스·DB·외부 접근 실패 | 실패 검사와 실제 리소스 상태 반환, Data/Traffic 진행 차단 |

**14 구현 (#56):** 복구는 같은 Revision 재실행이 아니라 실제 상태 확인 후 새 Revision 승인으로만 한다. 인프라 복구 Plan은 같은 환경 State의 기존 자원을 NO_CHANGE로 유지하고 부족한 자원만 만든다(`RECOVERY_CONTINUATION`). Workflow 취소는 `UNKNOWN`/`EXECUTION_CANCELLED`로 기록하며 삭제로 표시하지 않는다. 결정 F1~F9와 실측 증거는 [티켓 14](../../tickets/provisioning/14-recovery-integration.md)에 있다.

Temporal Retry가 외부 부수효과의 정확히 한 번 실행을 보장하지 않는다. 각 실행 단계는 작업 식별자·현재 대상 상태를 확인하고 같은 요청의 중복 실행을 억제한다. Production 취소·부분 실패의 자동 자원 정리 범위는 미정이며, 데이터가 들어간 PVC와 공유 네트워크는 특히 보존 여부를 먼저 판단한다. 트래픽 복귀는 Traffic Migration, DB 복구는 Data Migration의 설계 영역이다.

## 13. Verification — 설계 검증과 구현 우선순위

구현 시 우선순위 **제안**은 (1) 계약/Provider별 검증·Result, (2) 공통 Adapter·렌더링, (3) 공통 Helm Chart·Values·Secret 매핑, (4) K3s Agent 배포·실환경 검증, (5) AWS EKS Terraform, (6) GCP GKE Terraform, (7) Preview/Plan 고정·승인/Apply, (8) Temporal 연동, (9) 실패 복구·통합 검증이다. 승인 가드는 실제 실행 경로를 연결할 때 반드시 먼저 갖춰야 하므로 이 순서를 승인 없는 임시 Apply 허용으로 해석하지 않는다. 개발 작업의 완료 조건은 후속 `tasks.md`에서 정의한다.

검증할 계약 예시는 AWS/GCP/ON_PREM의 필수값 차이와 잘못된 CIDR·이미지 아키텍처, 동일 Spec의 대상별 Render 결과, Secret 원문 미노출, Sandbox와 Production의 공통 Chart·다른 Values, Plan Revision 변경 시 Apply 차단, 저장 Plan/Chart Digest 불일치 차단, Terraform 부분 실패 후 State 재조회, Helm 응답 유실 후 Release 재조회, Agent 재연결 후 중복 제출 방지, PVC Bound·DB 접속·앱 헬스 실패 시 `READY` 차단이다. 실제 EKS/GKE/K3s 검증은 각 환경에서 별도로 수행해야 하며 Sandbox 통과만으로 대체하지 않는다. 발표 5분 제약 때문에 사전 구축 범위를 정하더라도 Preview·승인·실제 검증이 수행된 것처럼 표시하지 않는다.

### 요구사항 대응표

| 요구사항 | 주요 설계 위치 |
| --- | --- |
| PROV-001~004 | §4 입력 모델·대상별 검증, §6 `.env`·권한 |
| PROV-005~006 | §2·§5 공통 패키지와 Sandbox/Production 책임 |
| PROV-007~009 | §7 Provider별 Terraform/Agent 경로 |
| PROV-010~013 | §6·§9 Helm·DB·설정·네트워크 |
| PROV-014~015 | §3·§8·§10 Preview·Plan 고정·승인 |
| PROV-016~017 | §8~§10·§12 실행·재시도·복구 |
| PROV-018~020 | §11 실제 검증·Result·후속 전달 |
| PROV-021 | §4·§6·§8·§10·§11 비밀 보호 |

## 14. 구현 전 결정이 필요한 사항

| 미확정 사항 | 권장 검토 방향·결정 이유 |
| --- | --- |
| 시연 중 EKS/GKE 생성 또는 사전 구축 | 5분 발표 시간과 실제 생성 시간을 측정해 구분. 사전 구축도 승인·실행의 의미를 명확히 표시 |
| Node 수·유형·신규 네트워크 | 비용·Quota·CIDR·권한 기준으로 Profile 결정. 기존 네트워크는 P0 제외 |
| Terraform Backend/Lock | 중앙 S3와 잠금 채택. 버전·권한·key·잠금 설정을 공통 실행 구성으로 확정 |
| Secret 저장·회전, Registry Pull | 대상별 접근 권한과 ECR 접근성, K3s NAT 환경까지 검토 |
| 도메인·TLS 발급 | 승인 Preview와 Ingress 외부 검증의 필수 여부 결정 |
| Agent 완료 통지·작업 인증 | 12에서 폴링+결과 보고+Activity 상태 조회로 확정. 콜백/비동기 완료와 장기 lease는 미채택 |
| Temporal 분할·Task Queue·Timeout/Retry | 실제 외부 작업 시간과 재시도 안전성 측정 후 결정 |
| Apply 실패·취소 후 자동 정리 | 공유 자원·PVC 데이터 보존과 비용 사이 정책 결정 |
| Sandbox 검증 이미지 | 동일 Digest 재사용은 ADR-003으로 결정. B-01은 공통 빌더, 계약 버전 전환·플랫폼별 검증 증거는 후속 통합 작업 |
| Production 기능 테스트 | 테스트 계정·데이터 변경·정리 범위 결정 |
| K3s 사전 준비·macOS Linux 환경 | Agent가 확인할 최소 클러스터/StorageClass 계약 결정 |
| 온프레미스 중계·WireGuard, Route 53·Envoy | 최종 채택 및 장기 운영 주체는 Migration 영역과 함께 결정 |
| S3 의존 앱의 P0 검증 | 사용자 객체 스토리지 자동 생성·이전 없이 시연할 기능 범위 결정 |

현행 로컬 Temporal/API 스캐폴드는 AWS→GCP 예시 흐름이고, 실제 EKS/GKE/K3s Provisioning을 구현하지 않는다. 이전 플랫폼 관리 앱·독립 K3s·Cloudflare 방향의 문서와 현행 정책 간 차이는 [제품 정책의 확인 필요](../../docs/product/policies.md)에 이미 기록돼 있다. 이 설계는 4단계 `spec.md`의 범위를 유지하며, 그 충돌을 임의로 해결하거나 기존 코드를 수정하지 않는다.

### Ticket 06 GCP Plan·Preview 경계

GCP 전용 명시 입력은 VPC 전체 CIDR이나 AWS AZ 목록을 받지 않는다. 신규 GCP VPC Network, 단일 리전 서브넷의 primary·Pod·Service CIDR, 별도 GKE control-plane /28, 접근 허용 CIDR, private node용 Cloud Router/NAT, 내부 firewall, 리전 GKE Standard cluster·분리된 Node Pool로 변환한다. Pod·Service·control-plane CIDR은 서로 겹치지 않는 RFC1918 범위로 검증한다. Node 서비스 계정은 동일 프로젝트에 이미 존재해야 하며 그 권한은 실제 환경에서 별도 확인한다. 앱·DB·Ingress·TLS·스토리지·ECR Pull은 이 인프라 Plan에 포함되지 않는다.

Terraform 1.10.5 이상과 잠금 파일의 Google Provider 7.x를 사용한다. 운영 Plan은 중앙 S3 State의 환경별 고정 key와 `use_lockfile=true`로 만들며, GCP Provider의 ADC와 S3 Backend의 AWS profile을 각각 검증한다. 검증용 `LOCAL_TEST`는 가상 프로젝트 `cmp-plan-local`, 임시 로컬 State, 무효 토큰과 `-refresh=false`로 실제 저장 Plan→Preview 변환을 실행한다. `LOCAL_PROJECT`는 지정 프로젝트와 ADC 주체를 대조한 뒤 보호된 로컬 State에서 같은 변환을 검증한다. 두 모드 모두 Preview 오류 `LOCAL_STATE_UNAPPROVABLE`로 승인 불가를 명시하고 Plan을 Apply에 재사용하지 않는다. 실제 GKE API·노드 서비스 계정/권한·중앙 S3 State 기반 최종 Plan 검증은 운영 승인 전에 별도 수행한다. [템플릿과 실행 예시](../../infra/terraform/gcp/README.md)를 따른다.

### Ticket 02 구현 결과

공통 PostgreSQL 16 Chart는 `infra/helm/postgres`에 구현했다. 공유 PostgresChartValues와 동일 구조의 schema를 사용하며 세 Provider와 Sandbox/Production 조합의 실제 Helm 렌더를 검사한다. 단일 StatefulSet·headless Service·보존 PVC를 생성하고 requests/limits와 PVC 용량을 분리한다. [입력·재현·미검증 범위](../../infra/helm/README.md)를 따른다. 원격 Artifact 저장과 배포/실제 PVC·DB 검증은 아직 수행하지 않는다.

### Ticket 03 구현 결과

앱 전용 `infra/helm/application`과 내부 ApplicationValues/환경변수 메타데이터 계약을 구현했다. 일반 설정·사용자 Secret은 ConfigMap/Secret 이름·키 참조, DB 연결은 명시적인 DATABASE 매핑으로 구분한다. 기존 DynamicBinding과 bind/verify 함수를 재사용해 승인된 host/secret_name 슬롯만 채운다. 공개 렌더 함수는 Helm 오류 원문을 숨기며 실제 배포/참조 조회는 수행하지 않는다. [계약·예제·책임 경계](application-rendering.md)를 따른다.

### Ticket 04 구현 결과

[k3s-agent.md](k3s-agent.md)의 로컬 폴링 Agent와 별도 k3d 환경에서 DB·앱 실제 배포, PVC/Ready/Service·SQL, 중복/재연결 및 정리를 검증했다. UID·작업 라벨로 테스트 대상에 묶고 Digest 고정 Helm 렌더 산출물을 적용한다. 운영 원격 Agent 프로토콜·인증·Helm Release 관리·Production 승인 연결은 후속 범위다. 학습용 minikube는 대상에서 제외한다.

### 빌드·Sandbox 순서 변경 (ADR-003, B-01)

앞선 단계의 `Sandbox → 최종 Build` 설명은 [ADR-003](../../docs/decisions/ADR-003.md)으로 대체한다. 계획 입력은 Sandbox가 실제 실행한 Build 참조·이미지 Digest·플랫폼/설정 범위와 Build 출처를 요구하며 통과 후 재빌드하지 않는다. [B-01](../../tickets/provisioning/b-01-image-build.md)은 S3→ECR 공통 빌더를 제공하며 현재 내부 v1 모델·테스트의 순서 전환은 후속 통합 작업이다.

### Ticket 18 Plan 생성·게시와 Preview 조회 (#98)

`cmp_worker.plan_publish.PlanPublisher`가 Migration별 Planning 입력 등록(`cmp_api.plan_inputs`)을 읽어 01A 검증 → 대상별 Plan·Preview → `publish_plan_revision`(입력 Digest 함께 저장)을 수행한다. ON_PREM은 Agent 등록·폐기·cluster UID를 확인한 뒤 04B 렌더·Chart 패키징(`cmp_providers.chart_package`)·12A Preview로 `WORKLOADS` Revision을, AWS/GCP는 05·06 저장 Plan으로 `INFRASTRUCTURE` Revision을 만든다. 같은 입력의 재실행은 최신 Revision을 재사용한다. 승인자는 `GET /provisioning/plans/{plan_id}/revisions/{revision}/preview` 또는 `GET /provisioning/migrations/{migration_id}/plan`으로 Preview와 승인용 Digest를 본다. Temporal 연결은 §10과 [temporal-flow](../../docs/architecture/temporal-flow.md)를 따른다. `.env` 값 주입(20), S3 ArtifactStore(21)는 후속이다. 결정은 [18 티켓](../../tickets/provisioning/18-onprem-plan-publish.md#착수-결정-2026-11-05).

### Ticket 22 AWS/GCP 워크로드 Plan 게시와 09·11 실행 연결 (#108)

`PlanPublisher.publish_workloads`가 실행된 AWS/GCP 인프라 Revision(최신·전 단계 `SUCCEEDED`·클러스터 refs 있음)과 등록의 워크로드 설정으로 별도 `WORKLOADS` Plan(`workload_plan_id_for`)을 게시한다. DB Values는 04B 렌더, 앱 Values는 09·11의 v1 템플릿으로 바꾸고 `helm template`으로 확인한다. 입력 Digest에 인프라 Revision·manifest Digest·refs를 넣어 같은 인프라 결과는 재사용, 새 인프라 실행은 새 Revision이 된다. `MigrationWorkflow`는 인프라 실행 뒤 이 Plan을 승인 대기시키고 09·11 결과를 13 `ProvisioningResult`로 남긴다. 결정은 [22 티켓](../../tickets/provisioning/22-cloud-workload-plan.md#착수-결정-2026-11-05).
