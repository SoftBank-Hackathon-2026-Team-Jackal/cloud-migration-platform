# 분석·매핑·Sandbox 검증 기능 요구사항

담당: 박승철 · 기준일: 2026-11-04(v2 구현 동기화) · 상태: 초안(사용자 검토 전)

## 1. 기능 개요와 목적

원본 AWS EKS 앱을 읽어 **무엇을 어떻게 쓰는지**를 증거와 함께 뽑고, 대상 환경에서 그대로 동작하는지 **Sandbox에서 증명**한 뒤, Provisioning Planning이 소비할 결과(분석 결과·검증된 이미지 Digest·테스트 묶음)를 넘긴다.

```text
대상 선택(Provider·capabilities) → 소스 확보 → 스캔(코드·매니페스트·인프라 병렬)
  → 요구사항(resources[]·연결·network_intent) → 매핑(카탈로그+Skill, AI 제안·근거 확인)
  → [사용자] 필수 기능 확정·잃는 것 승인 (BLOCK이 있으면 여기서 중단·보고)
  → 소스 묶음 → S3 → 공통 Build 모듈 → 이미지 Digest
  → Sandbox 워커가 구체 값 결정(Namespace·Release·StorageClass·Secret·NetworkPolicy)
  → 공통 모듈 render(SANDBOX) → helm upgrade --install → 준비 검사 → 테스트 → 판정
      실패: 설정·매핑(통신 의도 누락 포함) → 재render·갱신(빌드 없음)
            코드·빌드 → 수정·재빌드·갱신 / 변환기 → 중단·보고
  → 통과 시점의 최종 분석 결과·검증 결과·테스트 묶음 → Provisioning Planning
```

핵심 원칙: **등급은 서비스가 아니라 앱의 사용 방식이 정한다. AI가 판단하고 Sandbox가 증명한다. 판정은 AI가 아니라 객관적 신호(테스트 결과·호출 기록)가 내린다.**

이 문서는 기능이 충족할 조건과 수용 기준을 정의한다. 필드명·함수명·Activity 분할은 [plan.md](plan.md)의 제안이다. Provisioning과의 계약은 main에 구현된 v2를 따른다: [Planning 입력 v2](../provisioning/input-contract-v2.md)(`cmp_contracts.provisioning_v2`, 요구사항 형식 `RequirementsV2` 포함), [공통 렌더 v2](../provisioning/onprem-shared-deployment.md)(`cmp_contracts.shared_deployment_v2`), [B-01](../../tickets/provisioning/b-01-image-build.md). 제품 범위는 [제품 정책](../../docs/product/policies.md), 책임은 [도메인 경계](../../docs/architecture/domain-boundaries.md), 빌드·Digest 정책은 [ADR-003](../../docs/decisions/ADR-003.md)을 따른다.

## 2. 적용 범위와 전제

- **원본:** AWS EKS의 데모 앱. Deployment·Service·Ingress(ALB)·ConfigMap·Secret·PVC, PostgreSQL 16(클러스터 내 Pod. 첫 사이클은 RDS를 쓰지 않고 Pod로 한다고 결정, 2026-11-05), VPC·Subnet·Route Table·Security Group.
- **대상:** AWS EKS, GCP GKE, 온프레미스 K3s(P-02). 대상 선택은 **분석 시작 전에** 받는다. 선택에는 Provider와 **capabilities**(CPU 아키텍처, 사용 가능한 StorageClass, 이미지 접근 여부 등 실제 제약)가 들어 있다. 이름 붙은 Profile은 선택 기본값일 뿐 필수가 아니다.
- **첫 사이클 우선:** 컨테이너 실행, PostgreSQL, 환경변수, 네트워크, Ingress, 볼륨, Pod 신원. 리소스 종류는 `APPLICATION`·`POSTGRESQL`만 지원하고 나머지는 거절된다(공통 모듈 기준).
- **첫 사이클 제외:** S3 의존 코드 수정(사이클 완성 직후 고도화). S3는 원본에 남겨 두고 쓰는 서비스로 취급한다. 대응 없음 등급 서비스(DynamoDB 고유 기능, Step Functions, Lambda 중심, Kinesis)는 차단 사유로만 표시.
- **Sandbox 기반:** 미리 준비된 Kubernetes 클러스터와 그 VPC·오퍼레이터·출구 프록시·Registry를 재사용하고(P-04, PROV-006), 실행마다 Namespace와 워크로드만 만든다. VPC·외부 진입점·운영 크기는 Sandbox에서 만들지 않는다.
- **외부 의존:** 공통 Build 모듈(B-01), 공통 Chart·렌더 모듈(민서, 렌더 전용), 원본 접근 자격 증명 참조(민서 환경 연결), LLM 공급자(ADR 필요).

## 3. 책임 경계

| 영역 | 승철(이 기능) | 민서(Provisioning) |
| --- | --- | --- |
| 소스 | 클론·커밋 고정·소스 묶음·S3 업로드 | — |
| 빌드 | Build 모듈 호출, 결과 해석, 수정·재빌드 루프 | Build 모듈(S3 URI → build/push → Digest) |
| 필수 기능·손실 승인 | AI의 필수 여부 제안, 처리 계산, 사용자 확정·승인 대기(Workflow) | 승인 화면은 플랫폼(상헌) |
| 요구사항 | `RequirementsV2` 형식의 환경 중립 요구사항(`resources[]`·ID 연결·PostgreSQL 버전·확장·`network_intent`), 환경변수 예외 목록 | 요구사항 형식(`RequirementsV2`)과 대상 구성 해석 |
| DB 이미지 | 원본 DB 이미지 Digest를 `AnalysisReport`에 정보로 기록 | 대상 구성 단계에서 요구를 만족하는 Digest 선택·검증(그 단계의 소유자는 D-11) |
| 렌더 | 공통 렌더 v2(`render_shared_deployment_v2`) 호출 | 공통 렌더 v2(렌더 전용, 리소스 종류별 렌더러) |
| **SANDBOX 구체 값** | Namespace, Release 이름, StorageClass(capabilities에서), DB 비밀번호·앱 env Secret 생성, 변수별 `env_bindings`, NetworkPolicy | — |
| 적용·정리 | **SANDBOX**: `helm upgrade --install`·정리 | **PRODUCTION**: 승인 후 적용 |
| 검사 | 앱 기능 테스트 묶음과 실행기(`run_suite`), Sandbox 판정 | 배포 직후 준비 검사, 데이터 이전 후 테스트 묶음 실행 시점 결정(소울·상헌과) |

공통 모듈은 **렌더 전용**이고 구체 환경 값은 호출부가 정한다(민서 협의, 2026-11-03). 렌더 차트는 Sandbox와 Production이 공유한다. Sandbox Worker는 Production 자격 증명을 받지 않는다(PROV-006).

## 4. 개념적인 입력과 출력

### 4.1 입력

| 입력 | 내용 | 출처 |
| --- | --- | --- |
| 대상 선택 | Provider, capabilities(아키텍처·StorageClass·이미지 접근), 위치, 접근 참조 | API/사용자 |
| 코드 | GitHub 저장소, 앱 식별자(서비스 이름 또는 리소스 태그) | 사용자 |
| 환경변수 | 별도 업로드한 `.env`의 불변 참조 | 사용자 |
| 원본 접근 | 읽기 전용 자격 증명 참조 | 환경 연결(민서) |
| 필수 요구사항 | 필수 기능·운영 요구사항(HA·백업). 분석 중 AI가 제안하고 사용자가 확정(AV-022) | 사용자(승인 화면) |

### 4.2 출력

| 출력 | 받는 쪽 |
| --- | --- |
| 분석 결과(`AnalysisReport`): 사용 프로필, 매핑 행, 사전 점검, 변환 못 한 항목, 미검증 위험 | Planning(Preview 경고), 상헌(승인 화면), 소울(코드에서 보이는 DB 사용 단서) |
| 요구사항: `RequirementsV2`(`resources[]`, 리소스 간 ID 연결, PostgreSQL 버전·확장, `network_intent`) | Planning(`requirements_ref`, 정확한 바이트의 SHA-256) |
| 리소스별 소스 참조(`source_refs`): S3 URI·SHA-256 | Planning |
| 환경변수 예외 목록(`mapping_ref`): 원본 환경에 묶인 변수의 제외·대체, 누락 변수 | Planning, Sandbox·Production 워커 |
| Sandbox 검증 결과: 상태, `validated_workloads`(리소스별 Build 참조·Digest·플랫폼·소스 SHA-256), 요구사항 참조, 보고서, 테스트 묶음 참조, 정리 상태 | Planning |
| 코드 수정 시 패치 산출물(이전용 브랜치에 적용할 패치, PR 제목·본문, 소스 해시) | 사용자 |

분석 결과는 **Sandbox 통과 시점의 최종 버전**으로 발행한다. 루프 중 수정이 없으면 최초 결과와 같다.

## 5. 기능 요구사항과 수용 기준

### 입력과 소스 확보

#### AV-001 — 분석 요청 수신

저장소·앱 식별자·`.env` 참조·필요한 원본 접근 참조·대상 선택을 받아 검증한다. 공개 GitHub 소스에는 소스 인증 참조를 요구하지 않는다.

- 대상 선택(Provider·capabilities)이 없으면 분석을 시작하지 않는다.
- 자격 증명과 `.env`는 참조로만 받고 원문을 Workflow 입력·이력에 싣지 않는다.
- 누락·형식 오류를 항목별로 반환한다.

#### AV-002 — 소스 확보와 운영 커밋 고정

- 공개 GitHub 저장소를 선택한 데모 입력은 저장소 식별자·요청 커밋으로 전달한다. 분석 워커가 토큰 없이 소스를 확보하며, 소스 파일이나 GitHub 로그인 토큰을 Workflow 입력에 싣지 않는다.
- 운영 중인 이미지 라벨(`org.opencontainers.image.revision`)에서 커밋을 찾아 고정한다. 찾지 못하면 사용자 지정 커밋을 쓰고 결과에 "운영 버전 일치 미확인"을 남긴다.
- 격리된 작업 공간에 클론한다. 플랫폼 서비스 서버에서 사용자 코드를 빌드·실행하지 않는다.
- 같은 커밋 재요청은 기존 작업 공간을 재사용한다.

### 스캔

#### AV-003 — 코드 스캔과 `.env` 대조

- SDK 호출(서비스·동작), 환경변수 읽기, DB 드라이버·ORM 사용 위치, 외부 호스트 호출을 파일·줄 근거와 함께 추출한다.
- `.env`에만 있는 변수와 코드가 읽지만 `.env`에 없는 변수를 구분한다.
- 마이그레이션 코드가 PostgreSQL 확장을 스스로 만드는지(`CREATE EXTENSION`) 확인한다.
- 첫 사이클은 데모 앱 언어 하나만 지원하고, 지원하지 않는 언어는 "스캔 불가"로 표시한다.

#### AV-004 — 매니페스트·클러스터 리소스 스캔

- Deployment(이미지·자원·replica·probe·포트), Service, Ingress(ALB 어노테이션), ServiceAccount(IRSA), PVC·StorageClass, ConfigMap·Secret 참조를 읽는다. 저장소에 없으면 클러스터에서 읽고, Helm 차트는 렌더 후 읽는다.
- PostgreSQL이 Pod로 실행 중이면 **원본 DB 이미지 Digest**를 `AnalysisReport`에 정보로 기록한다(대상 구성 단계의 이미지 선택에 참고).
- ALB Ingress 어노테이션, IRSA 어노테이션, EBS StorageClass를 클라우드 의존 항목으로 감지한다.
- 사이드카·initContainer·CRD·모르는 어노테이션은 "변환 못 한 항목"으로 낸다. ACK 등 AWS 리소스를 나타내는 CR은 카탈로그에서 AWS 전용 서비스로 분류한다.

#### AV-005 — 인프라·네트워크 스캔과 통신 의도

- VPC·Subnet·Route Table·Security Group, ALB Listener·Target Group, IRSA 역할 정책, CloudTrail 관리 이벤트를 읽기 전용으로 조회한다. ALB 리스너는 인증서 유무·TLS 정책·HTTPS 리다이렉트까지, 클러스터 VPC의 로드밸런서를 오리진으로 쓰는 CloudFront 배포는 뷰어 프로토콜 정책과 별칭을 읽는다(티켓 28-A, CloudFront 권한이 없으면 스캔은 계속하고 `cloudfront_status`가 `DENIED`로 남는다).
- Security Group·Ingress·코드의 외부 호출을 **`network_intent`**로 바꾼다. 형식은 `RequirementsV2`의 목록이다: `{source_resource_id, destination_resource_id 또는 external_host, port, protocol}`. 리소스 간 통신은 `destination_resource_id`, 외부 목적지는 `external_host`로 쓴다.
- 외부 진입(인터넷 → 앱 포트·TLS)과 원본 CIDR은 v2 intent에 자리가 없으므로 `AnalysisReport`에 기록한다(외부 진입 표현은 D-15).
- 대상 CIDR·NAT·LB 종류·Security Group ID는 넣지 않는다.
- 권한 거부는 필요한 권한 이름과 함께 실패로 반환한다. 코드 스캔과 병렬로 실행한다.
- **범위 입력(티켓 06B):** 웹의 범위 선택에서 확정된 범위(`scope_id`)를 API가 저장된 기록에서 읽어 `AnalysisInput`의 `credential_ref`·`cluster_name`·`region`·`role_arns`(포함된 IAM 역할 후보만)·`scope`를 채운다. 웹이 값을 직접 보내지 않는다. 범위가 없으면 클러스터가 없어 인프라 스캔이 건너뛰어진다(결정 대기). 확정 범위는 `AnalysisReport.scope`에 기록하고, 스캔 필터·`MIGRATE`/`KEEP` 반영은 후속이다.
- **리소스 사용 추적(티켓 06C):** 범위의 후보는 선택한 것과 선택하지 않은 것을 모두(`selected`로 구분) 분석에 넘긴다. 스캔이 읽은 IAM 역할 정책(허용 액션, 와일드카드 여부)과 CloudTrail 요약은 `AnalysisReport.aws_usage`에 근거(`IAM_POLICY`, `CLOUDTRAIL`)와 함께 싣고, 스캔이 못 돈 경우는 사유(`NOT_COLLECTED`, `FAILED`)로 남긴다. 후보·코드 호출·CloudTrail을 대조해 `unverified_risks`에 `SCOPE_EXCLUDED_BUT_USED`(제외했는데 쓴다), `SCOPE_SELECTED_NO_USAGE`(선택했는데 흔적 없음), `USAGE_NOT_IN_SCOPE`(코드가 쓰는데 후보에 없음)를 올린다. CloudTrail은 데이터 이벤트와 오래된 기록을 보지 못하고 계정 전체를 보여 주므로, 호출 기록 없음은 쓰지 않음의 증거가 아니며 CloudTrail만으로 후보 밖 사용을 단정하지 않는다.

#### AV-006 — 요구사항·사용 프로필과 사전 점검

- 요구사항을 **`cmp_contracts.provisioning_v2.RequirementsV2`** 형식으로 만든다(새 모델을 정의하지 않는다). kind는 `APPLICATION`·`POSTGRESQL`이고, 앱의 DB 연결은 `env_bindings`의 `{resource_id, output}` 참조로 표현한다. DB 외 환경변수는 요구사항에 넣지 않고 `.env` 참조와 예외 목록으로 처리한다.
- PostgreSQL 리소스에는 버전(`"16"`)·확장·DB 이름·사용자·자원·`storage_gib`를 기록한다. DB 이미지는 요구사항에 넣지 않는다.
- 앱이 PostgreSQL 확장을 요구하면 현재 Planning이 거절하므로, 분석 단계에서 해당 기능을 차단 사유로 보고한다(확장 지원은 D-14).
- 기능별 사용 방식·호출 지점·근거, 상태 목록(DB·파일·세션·큐·백그라운드)을 담은 사용 프로필을 만든다.
- Dockerfile 유무·위치와 `COPY`·`ADD` 경로 누락(빌드 컨텍스트 점검)을 기록한다.
- 사전 점검은 **코드·매니페스트에서 보이는 단서**(세션 단위 기능, 확장 사용, 헬스체크 경로, 노출된 클라우드 기본 주소)를 낸다. 기본 키 없는 테이블·시퀀스·`wal_level` 등 DB 내부 상태 점검은 Data Migration(소울)과 담당을 정한다.

### 매핑

#### AV-007 — 카탈로그·Skill 기반 매핑

- Mapper는 카탈로그(사용 방식 어휘·탐지 규칙·등급 기준·표준 테스트) 안에서만 판단한다. 카탈로그에 없으면 "검증 안 된 제안"으로만 표시한다.
- 도메인별 Skill(`k8s-workload`, `db-postgres`, `network`, `config-env`)이 조사·판단·수정·테스트 방법을 제공한다.
- 출력은 스키마 검증을 통과한 JSON이다. 실패하면 재시도하고, 계속 실패하면 사람 검토로 넘긴다.

#### AV-008 — 등급·처리·검증 상태

- 기능마다 등급(`DIRECT` / `API_DIFF` / `PARTIAL` / `NONE`), 잃는 것, 처리(`PROCEED` / `APPROVAL` / `BLOCK`), 근거, 검증 상태를 남긴다. Enum은 대문자로 통일한다.
- 처리는 등급 × 필수 여부 표로 기계적으로 정한다. 필수 기능이 `NONE`이면 차단한다.
- 근거(파일·줄, 매니페스트 경로, 리소스 ID, 로그 ID)가 실제로 존재하는지 기계적으로 확인한다. 없는 근거를 단 판정은 `UNVERIFIED`로 남긴다.
- 검증 상태(`UNVERIFIED` → `EVIDENCE_CHECKED` → `SANDBOX_VERIFIED`)는 객관적 신호로만 올라간다.
- 각 의존 서비스에 **이전/원본 유지** 구분을 남긴다. Sandbox 출구 규칙이 이 구분을 쓴다(AV-013).

#### AV-022 — 필수 기능 확정과 손실 승인

"필수 기능을 잃지 않는다"를 사용자가 확인하는 단계다. 매핑 뒤, Sandbox 전에 한 번 멈춘다.

- Mapper가 매핑 행(기능의 사용 방식)별 필수 여부를 근거(호출 지점, 사용 빈도, 실제 호출 기록, 사용자 경로)와 함께 **제안**한다. 운영 요구사항(HA·백업·복원)도 원본 구성에서 찾아 제안한다.
- 사용자가 필수 여부를 **확정**한다. 확정 전에는 처리(`PROCEED`/`APPROVAL`/`BLOCK`)를 확정하지 않는다.
- 확정 후 처리를 다시 계산한다. `APPROVAL` 항목은 잃는 것을 항목별로 보여 주고 사용자가 **승인**해야 진행한다. 승인을 거절하면 그 기능을 필수로 바꿔 다시 계산한다(`BLOCK`이 될 수 있다).
- `BLOCK`이 하나라도 있으면 Sandbox로 가지 않고 사유와 대안을 보고한다.
- 확정·승인 기록(누가, 언제, 어느 분석 결과 버전에 대해)을 남긴다. Sandbox 루프에서 매핑이 바뀌면 바뀐 항목만 다시 승인받는다.
- 자동 승인은 없다. 응답을 기다리는 동안 Workflow는 대기하고, 상한 시간이 지나면 대기 상태로 남는다.
- 화면은 플랫폼(상헌), 승인 API는 FastAPI, 제안·계산·대기·재개는 이 기능이 맡는다.

### 빌드 연계

#### AV-009 — 소스 묶음 생성과 업로드

- **마스킹하지 않은** 소스(+ Fixer 수정본)를 묶는다. `.git`, `.env`, 비밀 파일을 빼고 `.dockerignore`를 적용한다.
- 아카이브 루트 = 빌드 컨텍스트, Dockerfile은 루트에 둔다.
- 재현 가능한 tar.gz(파일 순서 정렬, mtime·uid·gid 고정, gzip 헤더 mtime 0)로 만들어 같은 내용이면 같은 SHA-256이 나오게 한다.
- `s3://<버킷>/sources/<sha256>.tar.gz`로 올린다. 같은 해시가 이미 있으면 다시 올리지 않는다.
- 첫 빌드(분석한 커밋 그대로)는 항상 한다.

#### AV-010 — Build 모듈 호출과 결과 해석

- 공통 Build 모듈(`ImageBuilder(BuildContext).build(source_uri, source_version_id=...)`)에 작업 ID·리소스 ID·시도 ID·S3 URI·대상 플랫폼(capabilities 아키텍처)으로 빌드를 요청하고 `image_uri`·`image_digest`·`source_ref`·로그 위치를 받는다.
- 같은 `source_sha256` + 플랫폼으로 이미 성공한 빌드가 있으면 다시 요청하지 않고 기존 Digest를 쓴다.
- 실패 코드를 **수정 대상(Fixer) / 재패키징 / 재시도 / 사람 검토**로 분류한다.
- Sandbox 안에서 빌드하지 않는다(ADR-003). 현재 호출은 함수 직접 호출이며(`BUILD_*` 설정이 모두 있을 때 Sandbox 루프가 소스를 S3에 올리고 호출, 티켓 25), Worker 권한 배치와 전체 시간 상한은 결정 대기(D-12). 이미지를 못 만들면 Sandbox 없이 사람 검토(`BUILD:<코드>`)로 넘어간다.

### Sandbox 검증

#### AV-011 — Sandbox 구성·적용과 갱신

- Sandbox 워커가 구체 값을 정한다: Namespace·Release 이름(실행 ID 기준 규칙), StorageClass(capabilities에서 선택), DB 이미지(D-11 결과), DB 비밀번호 Secret(무작위 생성), 앱 env Secret(`.env`에서 예외 목록의 제외 변수와 DB 변수를 뺀 나머지), 변수별 `env_bindings`(DB 변수는 `{resource_id, output}`, 나머지는 `{secret_ref: {name, key}}`).
- 공통 렌더 v2(`SharedDeploymentRequestV2`, `environment: SANDBOX`)로 렌더 결과를 받고 `helm upgrade --install`로 적용한다.
- 실행(run)마다 Sandbox 하나를 띄우고, 시도마다 **제자리 갱신**한다. 설정·매핑만 바뀌면 재렌더·갱신, 이미지만 바뀌면 새 Digest로 갱신한다. Secret 내용만 바뀐 경우에도 Pod가 새 값을 쓰도록 재시작을 보장한다.
- 바꿀 수 없는 필드 변경, DB 메이저 버전 변경, 환경 손상일 때만 해당 부분을 다시 만든다.
- 시도마다 어떤 Digest·어떤 렌더 결과로 돌았는지 기록한다.
- 적용 후 준비 검사(Pod Ready, `SELECT version()`으로 PostgreSQL 버전 확인, SQL 접속, 앱 헬스)를 통과해야 테스트로 넘어간다.

#### AV-012 — 테스트 묶음

- 출처: (1) 카탈로그 표준 테스트 (2) 앱에 원래 있던 테스트 (3) AI 생성 테스트.
- 형태는 실행 중인 앱 API를 호출하는 HTTP 시나리오를 우선한다(언어 무관, 배포 이미지 그대로 검증).
- 표준 테스트의 쓰기·읽기 모양은 앱마다 다르므로 설정으로 받는다(`AppApi`). 모양이 없으면 헬스 테스트만 돈다. 멱등 쓰기 앱은 같은 쓰기를 다시 보내 같은 ID·본문이 오는지로 저장과 DB 재시작 후 유지를 확인하고, 재시작 직후 5xx인 동안은 요청을 다시 보낸다.
- 테스트마다 `READ_ONLY` / `WRITES_DATA`를 표시한다. 외부에 실제 부수효과(메일 발송, 결제 등)를 일으키는 테스트는 빼거나 테스트용 키로만 돌린다.
- AI 생성 테스트는 네 조건을 **모두** 만족할 때만 쓴다: (1) 기대값이 보증됨, (2) 필수 결과 assertion, (3) 의존성을 끊으면 실패, (4) 수정 전에 고정. 1번은 세 방법 중 하나로 충족한다. ① 속성 테스트: 모든 기대값이 그 테스트가 먼저 쓴 값이고(쓴 값을 그대로 읽음, 앞 단계 응답에서 저장한 값과 비교), 소스 근거(파일·줄)가 실제 소스에서 확인된다. ② 기록 테스트: 플랫폼이 첫 정상 기동 직후 기본 상태 앱에 읽기 전용 요청을 두 번 보내 같은 응답을 얻고(2xx, 변동 필드 제외) 그 응답을 기대값으로 삼으며, 소스 근거가 확인된다. ③ 원본 쪽 테스트 자원(D-09)에서 먼저 통과. 근거는 원본 자원이 있어도 소스가 있으면 확인한다. 모델이 정한 다른 리터럴 기대값은 ③ 없이는 쓰지 않는다.
- 테스트 생성(Tester)은 수정(Fixer)과 별도 세션에서 하고, Fixer는 테스트를 바꿀 수 없다.
- Tester는 설정 `ai_tests`가 켜진 때 첫 Sandbox가 정상 기동한 직후 한 번 호출한다. 소스(라우트 목록, 읽기 전용 소스 조각 도구, `test-authoring` 스킬)를 읽고 **쓰기 모양(`shapes`)과 읽기 전용 요청(`probes`)을 소스 근거와 함께 제안**한다. 기대값은 쓰지 않는다. 쓰기 모양은 플랫폼이 왕복·재시작 후 유지 테스트로 만들고, 읽기 요청은 응답을 기록해 테스트로 만든다. 원본 자원이 없으면 자유 형식 테스트는 받지 않는다(`ORIGIN_UNAVAILABLE`). 네 조건을 통과한 테스트만 새 묶음에 더해 해시로 고정하고, 거절된 테스트와 사유(`NO_EVIDENCE`, `EVIDENCE_MISMATCH <경로>`, `RECORD_UNSTABLE <필드>` 등)도 남긴다. 기록한 읽기는 같은 묶음의 쓰기 테스트보다 먼저 돌도록 ID를 `ai-0rec-`로 시작하게 하고, 기록 뒤 데이터를 기준 상태로 되돌린다(되돌리지 못하면 기록 테스트를 버린다: `RESET_FAILED`).
- 테스트 묶음은 SHA-256 이름의 불변 저장소(첫 사이클은 로컬 저장소, S3는 후속)에 저장하고, 대상 주소·인증 참조만 바꿔 실행하는 `run_suite(suite_ref, base_url, auth_ref, include)`를 제공한다. `include`로 읽기 전용 테스트만(`READ_ONLY`) 또는 전체를 고른다.

#### AV-013 — 통신 규칙과 실행 기록

- Sandbox 워커가 `network_intent`로 Sandbox 전용 NetworkPolicy를 만든다: 기본 차단, 리소스 간 항목(`destination_resource_id`) 허용, 외부 목적지 항목(`external_host`)은 출구 프록시 허용 목록에 추가, 외부 진입(분석 결과의 진입 정보) 허용, DNS 등 클러스터 기본 통신 허용.
- 원본에 **남겨 두는** 서비스(첫 사이클의 S3 등)로의 호출은 매핑 결과에 따라 허용하고 기록한다.
- **이전하는** 서비스의 원본 주소와 intent에 없는 외부 호출은 차단·기록한다. 이 차단으로 인한 실패는 분석의 의도 누락으로 분류한다(AV-015).
- 발견된 클라우드 의존 호출 지점의 실행 커버리지를 기록한다(100%도 기능 동등성 증명은 아님).

#### AV-014 — 판정

Verifier(정해진 검사기)가 판정한다. 통과 조건: 필수 테스트 통과, 지원하지 않는 호출 오류 없음, 실제 호출 ⊆ 매핑, 차단된 출구 시도 0. 테스트 실패는 Activity 실패가 아니라 판정 결과로 기록한다.

#### AV-015 — 실패 분류와 수정 루프

- 실패 원인을 명세·매핑(통신 의도 누락 포함) / 코드 / 빌드 / 변환기(render) / Sandbox 환경(INFRA)으로 나눈다. 판별은 입력 요구사항과 렌더 결과를 필드 단위로 대조해 한다.
- 명세·매핑 → Fixer가 요구사항 수정, 재렌더·갱신(빌드 없음). 코드·빌드 → Fixer가 코드 수정, 소스 묶음·재빌드·갱신. 변환기 → **수정하지 않고** 루프 중단, Provisioning 담당에 보고. Sandbox 환경(클러스터 접근 불가, 레지스트리 접근 실패 등) → 수정하지 않고 같은 입력으로 재시도, 연속 2회면 사람 검토.
- 수정 시도는 빌드 포함 최대 3회이고, INFRA 재시도는 횟수에 넣지 않는다. 3회 모두 실패하면 사람 검토 대기로 멈춘다.
- 시도마다 테스트 데이터는 초기화하고 테스트 케이스는 고정한다.

#### AV-016 — 정리

- 통과·실패·취소·Worker 장애 어느 경우에도 Sandbox 정리를 시도한다(Release 삭제, 보존 PVC 포함 Namespace 삭제).
- 실행 ID·만료 라벨을 붙여, 정리되지 못한 자원을 나중에 찾아 지울 수 있게 한다.
- 정리 상태(`CLEANED` / `FAILED` / `PENDING`)는 검증 상태와 따로 보고한다.

### 출력

#### AV-017 — 분석 결과 반환

- `AnalysisReport`(사용 프로필, 매핑 행, 사전 점검, 변환 못 한 항목, 미검증 위험, 기준 커밋, 원본 DB 이미지, 외부 진입, 원본 CIDR), 요구사항(`RequirementsV2`), 환경변수 예외 목록을 낸다. `AnalysisReport`·예외 목록은 이 기능의 계약이고, 요구사항은 Provisioning의 `RequirementsV2`를 그대로 쓴다. Provisioning v1 계약의 `AnalysisResult`와 이름을 구분한다.
- 요구사항은 JSON 바이트를 불변 저장하고 그 SHA-256을 `requirements_ref`로 넘긴다(Planning이 같은 바이트를 다시 검증한다).
- 비밀은 이름만 담는다. 값은 담지 않는다.
- **Sandbox 통과 시점의 최종 버전**으로 발행한다. 루프 중 수정된 요구사항과 소스가 반영된다.
- 별도 수정 설정 파일 산출물이 없으면 `config_ref`를 내지 않는다.
- 큰 결과(스캔 원본·렌더 결과)는 불변 저장 참조로 넘긴다.

#### AV-018 — Sandbox 검증 결과와 Planning 인계

- Planning 입력 v2(`PlanningRequestV2`)의 `analysis`·`builds[]`·`sandbox` 블록을 채울 수 있게 낸다: `validated_workloads`(리소스별 Build 참조·이미지 Digest·플랫폼·소스 SHA-256), 요구사항 참조, 보고서, `test_suite_ref`, 정리 상태. Build 결과는 불변 기록으로 저장해 `result_ref`를 발급한다.
- 통과 후 다시 빌드하지 않는다. 통과 Digest = Build Digest = Production 배포 Digest(P-10).
- 검증 이후 대상 선택이나 요구사항이 바뀌면 이전 검증을 그대로 넘기지 않는다.
- 구현(티켓 15): 통과한 실행은 `--planning` 설정이 있을 때 Planning 입력으로 조립·검증(`prepare_plan_v2`)해 등록한다. 웹 시작(`POST /migrations`에 `target`)은 같은 `PlanningSettings`를 API가 대상 Profile 카탈로그로 만들어 시작 입력 `planning`에 넣는다([Provisioning 44](../../tickets/provisioning/44-web-start-to-planning.md#착수-결정-2026-11-06)). 인계 쪽 코드는 그대로다. 빌드 증거가 없으면(미리 빌드한 이미지 경로) 등록하지 않고 `BUILD_EVIDENCE_MISSING`으로 멈춘다.
- Production에서는 배포 직후 준비 검사만 하고, 테스트 묶음은 데이터 이전 후·트래픽 전환 전에 실행한다(실행 시점은 해당 파트 결정).

#### AV-019 — 코드 수정 반환

- 루프 중 수정은 작업 공간에만 반영한다. 통과한 최종 수정만 **패치 산출물**로 반환하고 기본 브랜치에 직접 쓰지 않는다. GitHub App은 쓰지 않기로 결정했으므로(2026-11-05) 플랫폼은 push·PR 생성을 하지 않고, 패치를 받은 사람이 이전용 브랜치에 적용해 PR을 연다.
- 패치를 분석한 소스에 적용해 만든 소스 트리 해시가 통과한 소스의 해시와 같은지 대조하고, 다르면 패치를 내지 않는다.

### 보안 동등성 (초안, 사용자 검토 전)

원본과 대상을 같은 기준으로 측정해 "그대로 옮기면 보안이 낮아지는가"를 이전 전에 알리고, Sandbox에서 증명한다. 클라우드가 대신 처리하던 보호(HTTPS, DB 접근 제한 등)는 앱이 정상 동작해도 대상에서 조용히 사라질 수 있기 때문이다.

#### AV-023 — 보안 측정과 동등 판정

- **측정 기준(첫 사이클 4개):** 외부 HTTPS, DB 접근 범위, DB 전송 암호화, 운영용 API 외부 노출. 포트 관리, 네트워크 분리, 저장 암호화, 시크릿, 권한, 감사 로그, 백업은 같은 모델에 항목을 더하는 후속이다.
- **원본 측정:** 스캔 결과(매니페스트·인프라·코드·`.env` 키 이름)에서 기준별 원본 상태를 근거(`Evidence`)와 함께 기록한다. 근거가 없으면 "알 수 없음"으로 두고 통과로 간주하지 않는다. 비밀 값은 읽지 않는다(AV-020). 상태는 `PROTECTED` / `PARTIAL` / `UNPROTECTED` / `UNKNOWN` / `NOT_APPLICABLE`이고 `AnalysisReport.security`에 기준 순서대로 실린다(티켓 28-B). `sslmode`는 비밀이 아닌 출처(소스 코드, 매니페스트 리터럴, ConfigMap)에서 모드 값만 읽고, `.env`·Secret에만 있으면 `UNKNOWN`이다. 운영용 API는 관례적 경로를 **후보**로만 올리고 확정은 사용자가 한다.
- **판정:** 기준마다 원본 상태와 대상(예정) 상태를 비교해 `LOWER`(낮아짐) / `SAME` / `HIGHER` / `UNKNOWN`을 낸다. 원본부터 약한 항목은 약점(`WEAK_SOURCE`)으로 따로 표시한다. 같은 입력이면 같은 판정이 나온다.
- **동등 보장:** `LOWER`는 필수 기능의 `NONE`과 같이 취급한다. 보강으로 `SAME` 이상이 되지 않으면 `APPROVAL`(잃는 것을 보이고 사용자 승인) 또는 `BLOCK`이다. 자동 승인은 없다(AV-022).
- **약점 보강 제안:** 약점마다 대상에서 어떻게 고칠지(예: DB 접근을 앱 Pod로 제한, DB 연결에 SSL 강제, 운영용 경로 외부 차단)를 제안으로 표시하고 사용자가 승인한 것만 적용 대상이 된다. **보강을 대상 구성에 반영하는 방법은 Provisioning 계약 합의 전이므로 이 기능 범위에 넣지 않는다**(표시와 승인 기록까지).
- **Sandbox 증명:** 보안 프로브(비허용 Pod의 DB 접속 차단, 평문 DB 연결 거절, 확정된 경로의 외부 차단)를 테스트 묶음(`run_suite`)의 케이스로 실행하고, 앱의 기능 테스트가 계속 통과하는지 함께 확인한다. **프로브는 앱 중립 템플릿이다.** 앱 전용 케이스를 묶음에 미리 넣지 않고, 대상(리소스·경로)은 분석 결과와 승인 기록에서만 받으며 기대값도 거기서만 나온다. 대상이 분석에 없으면 케이스를 만들지 않는다(`NO_TARGET`). 프로브는 `READ_ONLY`이고, Production에서 같은 묶음을 다시 돌릴 수 있다.
- **수용 기준:** 원본이 HTTPS인데 대상 구성이 HTTP면 `LOWER`로 판정되고 승인 없이 진행되지 않는다. 원본 DB 접속이 평문이면 `WEAK_SOURCE`로 표시되고, 승인된 보강이 적용된 Sandbox에서 평문 접속이 거절된다. 근거 없는 측정은 `UNKNOWN`이다. 측정 결과는 `AnalysisReport`에 실려 승인 화면에 원본 값으로 전달된다.
- **전제:** 데모는 VPC당 앱 하나다(같은 VPC의 다른 앱 리소스를 걸러내지 않는다. 분석 범위 입력은 별도 결정).
- **제외(후속·타 담당):** 배포된 대상의 실제 측정과 원본 대비 최종 비교(Provisioning 배포 후), 비교 화면(상헌), 보강의 대상 구성 반영(민서), 감사 로그·백업·저장 암호화 항목.

### 보안과 실행 안정성

#### AV-020 — 비밀정보 보호

- `.env` 값, 자격 증명, 비밀 저장소 값이 결과·로그·Workflow 이력·LLM 입력에 나타나지 않는다.
- `.env` 실제 값은 Sandbox 워커가 **Secret을 만드는 순간에만** 읽고, 다른 곳에 저장하거나 출력하지 않는다. 분석 단계는 이름만 다룬다.
- 스캔은 읽기 전용이며 비밀 값 조회 API를 호출하지 않는다.
- LLM 입력 전 비밀처럼 보이는 문자열을 마스킹한다. 어떤 LLM에 무엇을 보낼지는 ADR-004가 정한다(Claude Sonnet 5.5, 마스킹한 증거 조각만 전송, 비용 상한은 코드로 강제).
- Sandbox는 운영 DB를 변경하지 않는다. DB 비밀번호는 Sandbox용으로 새로 만든다.
- 업로드된 소스 아카이브·`.env`·자격 증명을 저장소에 커밋하지 않는다.

#### AV-021 — Temporal 실행 규칙과 실패 대응

- Workflow는 결정적으로 유지하고 외부 I/O·LLM 호출은 Activity에서만 한다.
- 일시 오류(네트워크·스로틀링)만 재시도하고, 권한 거부·입력 오류는 재시도 없이 원인과 함께 실패한다.
- 재시도해도 소스 묶음·이미지·Sandbox가 중복 생성되지 않는다(내용 해시, 시도 ID, Release 이름 기준).
- 진행 상황(현재 단계·시도 번호·판정)을 Query로 조회할 수 있다.

## 6. 책임 제외 범위

| 제외 대상 | 담당 |
| --- | --- |
| 요구사항의 대상 구성 해석, 렌더 모듈·Chart, 대체 DB 이미지 선택 | Provisioning(민서) |
| 이미지 build/push 자체 | Build 모듈(민서) |
| Production 인프라 생성·배포·준비 검사 | Provisioning(민서) |
| 원본 접근·대상 환경 연결 발급 | Provisioning(민서) |
| DB 내부 상태 점검(기본 키·시퀀스·복제 설정), 데이터 복사·CDC·정합성 | Data Migration(소울) |
| 트래픽 전환, 승인 화면 | Traffic Migration·플랫폼(상헌) |

## 7. 통합 수용 조건

1. 데모 앱 저장소와 EKS 환경을 입력하면 `RequirementsV2` 검증을 통과하는 요구사항(`APPLICATION`·`POSTGRESQL`와 ID 연결)이, 분석 결과에 컨테이너 실행·PostgreSQL·환경변수·네트워크·Ingress 행이 각각 근거와 함께 나온다.
2. ALB Ingress·IRSA 어노테이션과 EBS StorageClass가 클라우드 의존 항목으로 감지되고, Pod DB의 원본 이미지 Digest가 분석 결과에 기록된다.
3. 앱 → DB 5432가 `network_intent`로 나오고, 외부 진입(443 → 앱 8080)과 원본 VPC CIDR이 분석 결과에 기록된다.
4. `.env` 대조 결과가 "안 쓰는 변수"와 "누락 변수"로 나뉜다.
5. 결과·로그·이력·LLM 입력 어디에도 비밀 값이 없다.
6. 일부러 틀린 설정(예: 포트 오류)을 넣으면 실패 → 수정 → 갱신 → 통과가 Temporal 이력에 남는다. intent에서 외부 목적지를 지우면 차단 → 의도 누락 분류 → 수정 → 통과가 남는다.
7. 3회 실패 시 사람 검토 대기로 멈추고 Sandbox 정리가 실행된다.
8. `PARTIAL`인 선택 기능이 있으면 사용자 승인 전까지 Sandbox로 가지 않고, 승인 기록이 분석 결과 버전과 함께 남는다. 필수 기능이 `NONE`이면 차단 사유가 보고된다.
9. 통과 결과의 이미지 Digest가 Build 결과 Digest와 같고, 투영한 입력이 `prepare_plan_v2` 검증을 통과한다.
10. 첫 사이클 분석·검증이 데모 환경에서 5분 안에 끝난다(Sandbox 기반과 첫 빌드는 미리 준비 가능).

## 8. 확인 필요

- (결정됨: 첫 사이클은 클러스터 안 Pod) 데모 앱 언어·API 목록·LLM 사용 여부·Dockerfile/매니페스트 위치 (소울)
- DB 이미지를 선택하는 "대상 구성 단계"의 소유자와, 그 결과를 Sandbox·Production이 같이 쓰는 방식 (민서, D-11)
- Build 호출을 어느 Worker 프로세스에서 하는지(권한 배치)와 전체 시간 상한 (민서, D-12)
- Build 실패 상세(`BUILD_FAILED`/`PUSH_FAILED` 분리, 정제된 출력 일부) — 급하지 않음 (민서, D-04)
- `*_ref` 저장 위치(S3 내용 해시 vs 업무 DB)와 Build 결과 저장·정규화 Activity 담당 (민서·팀, D-02)
- PostgreSQL 확장 지원 시점과 `CREATE EXTENSION` 실행 주체 (민서, D-14)
- `network_intent`에 외부 진입(인터넷 → 앱)을 표현하는 방법 (민서, D-15)
- Production(K3s)에서 NetworkPolicy가 필요해지면 공유 차트로 합칠지 (민서)
- DB 내부 상태 사전 점검 담당 (소울)
- 소스 묶음·테스트 묶음 S3 버킷 소유·정리 규칙, Sandbox 이미지 보존 기간 (민서·승철)
- Sandbox 기반 클러스터를 누가·언제 만들어 두는지 (민서·승철)
- LLM 공급자·모델·코드 전송 범위 ADR (승철·팀)
- 스캔 권한 목록(EKS 읽기, SSM 사용 여부) 전달 (승철 → 민서)
