# Implementation plan: 분석·매핑·Sandbox 검증

[spec.md](spec.md)의 `AV-001`~`AV-022`를 Python 3.11, Pydantic v2, Temporal, Helm으로 구현하기 위한 **설계 제안**이다. 함수명·필드명·Activity 분할은 구현하며 확정한다. Provisioning과 맞물리는 계약은 main에 구현된 v2를 따른다: [Planning 입력 v2](../provisioning/input-contract-v2.md)(`cmp_contracts.provisioning_v2`), [공통 렌더 v2](../provisioning/onprem-shared-deployment.md)(`cmp_contracts.shared_deployment_v2`, `cmp_providers.shared_deployment_v2`), [B-01](../../tickets/provisioning/b-01-image-build.md)(`cmp_worker.image_build`). v1 문서(`input-contract.md`, `shared-contracts.md`)는 역사적 기록이다.

## 0. 계약 협의와 v2 구현 상태 (2026-11-04)

| 항목 | 결정 | 상태 |
| --- | --- | --- |
| 공통 모듈 역할 | 렌더 전용. 구체 환경 값은 호출부가 정함. 차트는 Sandbox·Production 공유 | 합의 |
| 렌더 입력 구조 | `SharedDeploymentRequestV2`: `resources[]`(id·kind) + ID 참조. `APPLICATION`·`POSTGRESQL`만 | 구현됨(#24) |
| 요구사항 형식 | `RequirementsV2`(`resources[]`, `network_intent[]`). 분석이 이 모델로 출력 | 구현됨(#22) |
| Planning 입력 | `PlanningRequestV2`(`analysis`·`builds[]`·`sandbox.validated_workloads`·`test_suite_ref`·`capabilities_ref`) | 구현됨(#22) |
| 대상 정보 | 필수 Profile 대신 capabilities(아키텍처·StorageClass·이미지 접근). Profile은 선택 기본값 | 구현됨(#22) |
| `.env` | 워커가 `.env`와 예외 목록을 처리해 변수별 `env_bindings`·Secret 생성. 렌더러에는 Secret 이름·키 또는 리소스 출력 참조만 | 수용 |
| `config_ref` | 별도 수정 설정 파일 산출물이 있을 때만 | 수용 |
| `source_ref` | 리소스별 S3 URI·SHA-256 | 수용 |
| `network_intent` | 분석이 기록, 각 워커가 환경 정책으로 변환. Sandbox 전용 규칙은 별도 | 수용 |
| 배포 후 테스트 | 배포 직후 준비 검사만, 테스트 묶음은 데이터 이전 후 | 수용 |
| `test_suite_ref` | S3 SHA-256(`S3ArchiveRef`), `run_suite` 제공 | 계약 구현됨, `run_suite`는 승철 |
| PostgreSQL 확장 | 비어 있지 않으면 Planning이 거절 | 구현됨. 지원은 D-14 |
| DB 이미지 | 요구사항에 넣지 않음. 대상 구성 단계에서 선택·검증. 원본 이미지는 분석 결과에 정보로 | 방향 확정, 단계 소유자 **D-11** |
| Build 모듈 | `BuildContext`에 `resource_id`, `build(source_uri, source_version_id=...)`, 반환 `image_digest`·`source_ref`·`log` | 구현됨. Worker 권한 배치·전체 시간 상한은 **D-12** |
| 외부 진입 표현 | v2 `network_intent`는 출발지가 리소스여야 해서 인터넷 → 앱을 표현할 수 없음 | **결정 대기 D-15** |

## 1. Affected modules and contracts

```text
packages/contracts/src/cmp_contracts/
  analysis.py          # 새로: AnalysisRequest, Evidence, MappingRow, UsageProfile, PrecheckReport,
                       #       ExternalEntry, EnvExceptions, AnalysisReport
                       #       (요구사항은 provisioning_v2.RequirementsV2를 import해 사용)
  verification.py      # 새로: SourceArchive, BuildAttempt, SandboxAttempt, Verdict,
                       #       SandboxVerification, PlanningRequestV2 투영 함수
packages/agents/src/cmp_agents/      # 새로: 에이전트 본체 (Temporal 의존 없음)
  llm.py  runtime.py  tools.py  skills.py  masking.py   # 실행 기반(§2.11)
  mapper.py  tester.py  fixer.py                         # 역할별 에이전트
  prompts/                                               # 역할별 시스템 프롬프트(버전 관리)
packages/scanners/src/cmp_scanners/  # 새로: 결정적 스캐너 (Temporal 의존 없음)
  source.py  code_python.py  manifests.py  aws_infra.py  network.py  profile.py  precheck.py
packages/testsuite/src/cmp_testsuite/ # 새로: 테스트 묶음 형식과 run_suite (Production 재사용)
catalog/               # 카탈로그 YAML (사용 방식 어휘·탐지 규칙·등급 기준·표준 테스트)
skills/                # Skill 지침·보조 스크립트 (k8s-workload, db-postgres, network, config-env)
apps/worker/src/cmp_worker/
  activities/analysis.py      # 스캔·매핑 Activity (얇게 감싸기)
  activities/sandbox.py       # 새로: 묶음·빌드 호출·구성·적용·테스트·판정·수정·정리 Activity
  sandbox/                    # 새로: 구체 값 결정, Secret·NetworkPolicy 생성, Helm 적용·정리, 출구 기록
  workflows/analysis.py       # 새로: AnalysisWorkflow (자식)
  workflows/sandbox.py        # 새로: SandboxVerificationWorkflow (자식)
  workers/analysis.py, workers/sandbox.py
tests/analysis/, tests/sandbox/, tests/fixtures/analysis/
```

- 분석 결과 모델 이름은 `AnalysisReport`로 한다. Provisioning v1의 `cmp_contracts.provisioning.AnalysisResult`와 혼동하지 않기 위해서다.
- 요구사항은 새로 정의하지 않고 `cmp_contracts.provisioning_v2.RequirementsV2`(`ApplicationRequirement`, `PostgresRequirement`, `NetworkIntent`)로 출력한다. Planning에는 `PlanningRequestV2` 형태로 투영한다.
- Enum 값은 대문자로 통일한다(`DIRECT`, `PROCEED`, `SANDBOX_VERIFIED`, `PASSED`, `CLEANED`).
- 새 Python 패키지는 `pyproject.toml`의 `packages`·`package-dir`에 등록한다.
- Provisioning 모델(`provisioning`, `provisioning_v2`, `shared_deployment*`)은 **수정하지 않는다**. 부족한 점은 민서에게 요청한다.
- Queue: 기존 `analysis`를 쓰고 Sandbox Activity는 별도 `sandbox` Queue를 쓴다(D-05 결정). Q-01은 Queue 상수·Worker 실행기 등록만, 02는 분석 Workflow 연결만, 02B는 자식 Workflow 등록·호출만 공유 파일에서 변경한다. 기존 Queue 이름과 다른 도메인의 동작은 유지한다.
- Q-01의 기동 전용 scaffold Activity는 외부 I/O나 실제 Sandbox 작업을 수행하지 않는다. 별도 Queue에서 완료되는지 확인하며 기존 smoke 성공만을 Sandbox Worker 기동의 증거로 삼지 않는다.

## 2. Design and data flow

```text
MigrationWorkflow (migration)
 ├─ AnalysisWorkflow (자식, ID: analysis-<migration_id>, migration queue)
 │   fetch_source ──┬─ scan_code ───────┐
 │                  ├─ scan_manifests ──┤ 병렬 (asyncio.gather)
 │                  ├─ scan_aws_infra ──┤
 │                  └─ scan_network ────┘
 │   build_requirements → precheck → map_usage(LLM, 필수 여부 제안 포함) → check_evidence
 │   [Signal 대기] 사용자 필수 기능 확정 → compute_treatment
 │   [Signal 대기] APPROVAL 항목이 있으면 사용자 손실 승인 → record_review
 │   (BLOCK이 하나라도 있으면 여기서 중단·보고)
 ├─ SandboxVerificationWorkflow (자식, migration queue → sandbox activities)
 │   generate_tests(LLM, 별도 세션) → validate_tests → 고정(lock)
 │   package_source → request_build → BuildAttempt
 │   prepare_sandbox (Namespace·Release·StorageClass·Secret·env_bindings·NetworkPolicy)
 │   반복 attempt = 1..3:
 │     render_sandbox → apply_sandbox → readiness_check
 │     reset_test_data → run_tests → judge
 │     통과 → break
 │     실패 → classify_failure
 │        SPEC(통신 의도 누락 포함) → fix_mapping(LLM) → (다음 시도: 재render, 같은 Digest)
 │        CODE/BUILD → fix_code(LLM) → package_source → request_build → (새 Digest)
 │        RENDER → 중단, Provisioning 담당 보고
 │        INFRA → 수정 없이 같은 입력으로 재시도(시도 횟수에 넣지 않음), 연속 2회면 사람 검토
 │   3회 초과 → 사람 검토 Signal 대기
 │   통과 → publish_final_report (최종 AnalysisReport·요구사항 버전 발행)
 │   finally: teardown_sandbox
 └─ AnalysisReport + RequirementsV2 + SandboxVerification 참조 → PlanningRequestV2 조립·prepare_plan_v2(민서)
```

- 자식 Workflow로 분리하는 이유: 독립 취소·정리(finally teardown), 시도별 이력 구분, 사람 검토 대기. temporal-flow.md §7의 분리 근거(별도 취소·정리)에 해당한다.
- 분석 스캔·매핑과 08B 검토 Signal·Query는 `workflows/analysis.py`의 `AnalysisWorkflow` 자식에 둔다. 현재 API에서 `migration_id`는 부모 Workflow ID이며 요청의 `project_id`와 구분한다. 자식 ID는 `analysis-<migration_id>`로 고정하고 재분석은 `analysis_ref` 버전으로 구분한다. 다른 버전에 대한 검토 Signal은 거절한다.
- 02는 `workflows/migration.py`의 분석 자식 호출·결과·실패·취소 처리와 Workflow Worker의 자식 등록, `workers/analysis.py`의 분석 Activity 등록을 변경한다. 02B는 `workflows/migration.py`의 Sandbox 자식 호출과 Workflow Worker의 자식 등록, `workers/sandbox.py`의 Sandbox Activity 등록을 변경한다. 08B는 분석 자식 내부만 수정한다. 부모 변경은 해당 연결에 한정하고 PR 설명에 변경 범위와 replay 결과를 기록해 민서님께 공유한다.
- 14와 15도 부모 `workflows/migration.py`를 변경할 수 있다. 14는 종료 상태 값 반환(검토 대기는 진행 중 플래그, 종료 상태는 완료·실패·취소), 15는 분석·Sandbox 결과 참조를 Planning 입력으로 인계하는 부분에 한정한다. 두 티켓 모두 변경 범위, 기존 이력 replay 결과, Worker 배포 영향을 PR 설명에 기록해 민서님께 공유한다. 14B 구현으로 `needs_review`·`stopped_render`는 새 실행에서 `failed`(원인은 `failure_code`)로 대체되고 검토 대기는 `review_pending` 플래그가 되었다(`sandbox-terminal-v1` 패치, 이전 이력은 그대로 replay). API SSE 종료 조건에 `failed`를 추가하는 일은 민서님 요청 사항이다.
- 부모·자식 Workflow는 기존 `WORKFLOW_QUEUE`(`migration`), 분석 Activity는 `ANALYSIS_QUEUE`, Sandbox Activity는 `SANDBOX_QUEUE`를 사용한다. 기존 승인·취소 동작과 기존 Workflow 이력 replay를 확인하고 필요하면 Temporal의 결정적 버전 분기를 적용한다.
- 02 골격은 `analysis-child-v1` 패치로 기존 이력을 구분한다. 자식 실패는 부모에 전파하고, 부모의 `request_cancel` Signal은 자식 취소 완료를 기다린 후 `cancelled`를 반환한다. 부모의 외부 취소 시에도 자식에 취소를 요청한다. 부모가 종료될 때 미완료 자식의 ParentClosePolicy는 `TERMINATE`다. 스캔 결과와 최종 보고서는 실제 분석 증거가 아닌 작은 fixture다.
- 02B 골격은 별도 `sandbox-child-v1` 패치로 기존 부모 이력을 보존하며 `sandbox-<migration_id>` 자식을 호출한다. 자식과 부모의 취소는 정리 Activity를 실행한다. 검토가 필요한 경우 정리를 완료한 뒤 `NEEDS_REVIEW`에서 Signal을 기다린다. 골격의 검토 Signal은 검토 종료만 나타내며 통과나 운영 승인으로 취급하지 않는다. `PASSED` 이외에는 부모가 다음 scaffold 단계로 진행하지 않는다. 빌드·판정·수정·정리는 무해한 mock이며 실제 Sandbox 증거가 아니다.
- 02의 Temporal 경계는 기존 기본 Data Converter를 유지하고 Activity의 JSON 호환 `dict`·참조를 주고받는다. Pydantic 모델은 Activity 등 경계에서 `model_validate`로 검사하고 `model_dump(mode="json")`으로 직렬화한다. Workflow에는 대용량 결과나 비밀값을 넣지 않고 참조·작은 메타데이터만 전달한다. 02의 테스트는 실제 기본 Converter와 Workflow·Activity 경계에서 fixture 왕복 및 잘못된 입력 거절을 확인한다. 공유 API Client의 Converter는 변경하지 않는다.
- 분석 Activity 통합(Ticket 19): `AnalysisWorkflow.run`은 문자열이면 02의 scaffold 경로, `AnalysisInput` dict이면 실제 경로를 탄다(부모가 아직 문자열을 넘기므로 기존 흐름과 이력은 그대로). 실제 경로는 스캔 3종(코드·매니페스트·AWS)을 병렬로 돌리고 `build_requirements`가 코드·매니페스트·AWS 결과로 요구사항과 통신 의도(`scan_network` 역할 포함)를 만든 뒤 사전 점검 → 매핑 → 근거 확인 → 검토 게이트로 이어진다. 스캔 원본·요구사항 바이트·매핑 행·보고서는 해시 이름 로컬 저장소(`ArtifactStore`, D-02)에 두고 Activity 사이에는 참조만 넘긴다. 분석 Activity는 로컬 작업 공간 때문에 한 Worker 프로세스에서 돈다. 설정 `ANALYSIS_ARTIFACT_STORE_DIR`과 `ANALYSIS_WORKSPACE_ROOT`가 모두 있을 때만 실제 Activity를 등록한다.
- Workflow에는 참조와 작은 메타데이터만 둔다. 스캔 원본·렌더 결과·테스트 보고서·테스트 묶음은 S3에 `<sha256>` 이름으로 저장하고 참조로 넘긴다.
- Planning은 앞 단계를 다시 실행하지 않는다. 최상위 Workflow가 결과 참조를 모아 한 번에 넘기고, Planning이 저장소에서 내용을 읽어 대조한다.

### 2.1 소스 확보 (AV-002)

1. 공개 GitHub 저장소를 고른 데모 경로는 기존 `cmp_scanners.source.fetch_source(..., token=None)`를 재사용한다. 비공개 저장소용 토큰 클론·GitHub App은 후속 범위다. 웹은 소스 파일 대신 저장소 ID·`owner/repo`·요청 SHA를 보내고, API는 공개 여부·ID·커밋을 확인한다. 현재 API와 `fetch_source` Activity 사이의 연결은 [18](../../tickets/analysis-verification/18-public-github-source.md)에서 담당자와 입력 계약을 맞춘 뒤 구현한다.
2. 운영 커밋: EKS의 실행 이미지 → ECR 이미지 설정의 OCI 라벨에서 커밋. 실패 시 사용자 지정 커밋 + `commit_verified=false`.
3. 작업 공간: Worker의 격리 디렉터리 `<workdir>/<migration_id>/<commit>`. 원본(빌드용)과 마스킹본(LLM용)을 분리 보관한다.
4. `.env`는 분석 단계에서 이름만 파싱한다. 값은 Sandbox 워커가 Secret을 만들 때만 읽는다(§2.6).

### 2.2 스캔 (AV-003~AV-006)

| 스캐너 | 방식 | 결과 |
| --- | --- | --- |
| `code` | 언어 공통 `CodeScanner` 인터페이스 + 언어별 어댑터. 찾는 것: 클라우드 SDK 클라이언트·호출, 환경변수 읽기, DB 드라이버·연결 URL, 외부 호스트 호출. 첫 어댑터 언어는 D-07 결정 후 정한다(미정). 언어와 무관한 부분(언어 감지와 미지원 파일 "스캔 불가" 표시, SQL 마이그레이션의 `CREATE EXTENSION`, `.env` 이름 대조)은 먼저 만든다 | 호출 지점 목록(파일·줄) |
| `manifests` | YAML 파싱, Helm 차트는 `helm template` 후 | 리소스별 클라우드 의존, 원본 DB 이미지 Digest(정보) |
| `aws_infra` | boto3 읽기 전용(EKS, EC2 VPC/Subnet/RouteTable/SG, ELBv2, IAM role policy, CloudTrail LookupEvents) | 리소스 설정 |
| `network` | SG·Ingress·코드 외부 호출 → `NetworkIntent` 목록 + 외부 진입·원본 CIDR | 통신 의도 |
| `profile` | 위 결과 병합 + Dockerfile 파싱(`COPY`/`ADD` 경로 존재 확인) → `UsageProfile`, `RequirementsV2` | 요구사항 |
| `precheck` | 코드·매니페스트에서 보이는 단서(세션 기능, 확장, 헬스체크, 노출 주소) | `PrecheckReport` |

교차 해석: 코드 + CloudTrail 일치 = 확실히 사용, 코드에만 = 드문 경로, 권한에만 = 미사용 권한(거버넌스 항목). CloudTrail은 S3 데이터 이벤트를 기본 기록하지 않는다는 한계를 결과에 표시한다.

자격 증명은 `get_session(credential_ref)` 인터페이스 뒤에 둔다. 개발 중에는 SSO 프로필, 이후 환경 연결(민서)이 발급한 참조로 교체한다. 개발용 역할에도 스캔용 정책(읽기 전용, 비밀 값 조회 금지)을 그대로 붙이고 그 정책을 권한 목록으로 전달한다.

요구사항 형식은 `RequirementsV2`(구현 기준)다:

```yaml
contract_version: "2"
resources:
  - id: postgres-main
    kind: POSTGRESQL
    version: "16"
    extensions: []          # 비어 있지 않으면 Planning이 거절(D-14)
    database: { name: appdb, username: appuser }
    cpu_millicores: 250
    memory_mib: 512
    storage_gib: 5
  - id: api-main
    kind: APPLICATION
    replicas: 1
    cpu_millicores: 250
    memory_mib: 256
    port: 8080
    health_path: /health
    env_bindings:           # DB 연결만. 다른 변수는 .env 참조와 예외 목록으로 처리
      DB_HOST: { resource_id: postgres-main, output: host }
      DB_PASSWORD: { resource_id: postgres-main, output: password_secret_ref }
network_intent:
  - { source_resource_id: api-main, destination_resource_id: postgres-main, port: 5432, protocol: TCP }
  - { source_resource_id: api-main, external_host: api.example.com, port: 443, protocol: TCP }
```

- 요구사항 JSON은 저장한 **정확한 바이트**의 SHA-256을 `requirements_ref`로 쓴다. 키 순서 등 직렬화를 고정해 같은 내용이면 같은 해시가 나오게 한다.
- v2 intent에 자리가 없는 정보는 `AnalysisReport`에 둔다: 외부 진입(`{protocol: HTTPS, port: 443, target_resource_id, target_port}`), 원본 CIDR, 외부 목적지별 `MIGRATE`/`KEEP` 구분. 외부 진입을 v2 intent로 옮길지는 D-15.
- 원본 DB 이미지 Digest는 `AnalysisReport`에 정보로 남기고, 요구사항에는 넣지 않는다(이미지 선택은 대상 구성 단계, D-11).
- 앱이 확장을 요구하면 Planning이 거절하므로, 분석 단계에서 해당 기능을 `BLOCK`으로 보고한다.

### 2.3 매핑 (AV-007, AV-008)

- 카탈로그 항목: `capability`, `usage_vocabulary`, `detect_rules`, `grade_rules`, `known_differences`, `standard_tests`.
- Mapper 입력: 마스킹된 증거 요약 + 해당 Skill 지침 + 카탈로그 후보. 출력: `MappingRow[]`(JSON 스키마 강제). 행마다 분석 버전 안에서 고유한 `id`를 부여한다. 같은 capability에도 여러 행이 있을 수 있다. 같은 사용 방식의 행은 분석 버전이 바뀌어도 같은 `id`를 유지한다(이전 버전의 행 ID를 Mapper 입력으로 넘기고, 새 사용 방식에만 새 ID). 그래야 루프 중 바뀐 행을 ID로 비교해 그 행만 다시 승인받을 수 있다.
- 처리 표(기계적): `DIRECT`·`API_DIFF` → `PROCEED`. `PARTIAL` → 필수면 대체 방법 증명 시 진행 아니면 `BLOCK`, 선택이면 `APPROVAL`. `NONE` → 필수면 `BLOCK`, 선택이면 `APPROVAL`.
- 각 의존 서비스에 `MIGRATE` / `KEEP`(원본 유지)을 남긴다. 첫 사이클의 S3는 `KEEP`.
- `check_evidence`: 근거의 파일·줄 존재, 매니페스트 경로, 리소스 ID 조회. 실패한 행은 `UNVERIFIED`로 둔다.

**필수 기능 확정과 손실 승인 (AV-022)**

```text
map_usage ──> MappingRow[required_proposed, required_reason]
   │
   ▼  Signal: confirm_requirements {migration_id, analysis_ref, required: {row_id: bool}, operational: [...], actor}
compute_treatment ──> treatment 확정 (등급 × 확정된 필수 여부)
   │  BLOCK 있음 → 중단·보고
   │  APPROVAL 있음 ↓
   ▼  Signal: approve_losses {analysis_ref, approvals: {row_id: APPROVED | REJECTED}, actor}
   │  REJECTED → 해당 기능을 필수로 바꿔 compute_treatment 재실행
record_review ──> ReviewRecord(불변 저장, analysis_ref 버전에 묶음) → Sandbox 진행
```

- FastAPI는 분석 자식(`analysis-<migration_id>`)의 검토 상태를 먼저 Query하고, 자식이 없거나 검토 대기가 아니면 Signal 없이 오류를 반환한다. 검토 대기일 때 권한 확인·업무 DB 기록을 거쳐 자식에 Signal을 직접 보낸다. 구체적인 API 경로·오류 응답·화면은 상헌님과 합의한다(현재 OpenAPI에는 없음, 추가 시 `make openapi`). Query 후 상태가 바뀔 수 있으므로 자식도 Signal의 상태·분석 버전을 검증한다.
- Workflow는 Signal을 기다리며 Query로 "검토 대기 중"과 검토할 항목을 보여 준다. 자동 승인은 없다.
- Signal이 다른 분석 버전(`analysis_ref`)을 가리키면 거절한다. 확정·승인 키는 해당 버전의 행 ID이며, Sandbox 루프에서 매핑이 바뀌면 바뀐 행만 다시 승인받는다(`publish_final_report` 전에 확인).
- 데모 시간을 위해 화면 없이도 진행할 수 있게, 같은 Signal을 CLI 스크립트로 보낼 수 있게 한다.
- 구현 메모: Signal은 호출자에게 오류를 돌려주지 못하므로 거절한 Signal은 무시하고 거절 기록(코드만, 내용 없음)을 Query의 `rejections`에 남긴다. `BLOCK`이면 부모 공유 파일을 바꾸지 않고 멈추기 위해 자식이 `ANALYSIS_BLOCKED`로 실패하고(부모가 자식 실패를 전파), 사유·대안은 종료 후에도 Query로 조회한다. 상태 기계는 `cmp_worker.review_gate.ReviewGate`, 전송은 `cmp_worker.review_client`(Query 후 Signal)와 `scripts/review_signal.py`다.

### 2.4 소스 묶음 (AV-009)

```python
def build_archive(root: Path, files: list[Path]) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for p in sorted(files):
                info = tar.gettarinfo(root / p, arcname=str(p))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(root / p, "rb") as f:
                    tar.addfile(info, f)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()
```

- 파일 목록: `.dockerignore` 적용 후 `.git/`, `.env*`, 인증 파일 패턴 제외. 일반 파일만 넣는다. B-01이 심볼릭 링크·경로 이탈·`.env`를 `UNSAFE_ARCHIVE`로 거절하므로 같은 규칙을 묶기 전에 미리 검사한다.
- Dockerfile이 하위 폴더에 있으면 컨텍스트 범위를 정해 루트로 재배치한다. Dockerfile이 없으면 Fixer가 생성(코드 수정과 같은 취급).
- 업로드 전 `HeadObject`로 같은 키가 있으면 생략한다.

### 2.5 Build 모듈 연계 (AV-010)

```python
context = BuildContext.model_validate(
    {
        "migration_id": migration_id,
        "resource_id": resource_id,  # APPLICATION ID
        "attempt_id": f"{run_id}-b{n}",  # 코드 변경마다 새 n, 같은 빌드 재시도는 같은 n
        "ecr_repository": settings.ecr_repository,
        "platform": capabilities.architecture,  # 대상 capabilities에서 결정
        "log_bucket": settings.build_log_bucket,
    }
)
# B-01 구현 기준 함수 호출. 반환: image_uri, image_digest, source_ref{uri, version_id, sha256}, log
outcome = ImageBuilder(context).build(source_uri, source_version_id=None)
```

- **호출 위치(D-12):** B-01은 루프가 함수를 직접 호출하는 설계라, 그대로면 호출하는 Worker에 Docker·ECR push 권한이 필요하다. `build` Queue의 `prepare_image` 스캐폴드를 실제 빌드 Activity로 바꾸는 방안을 질문했다. 결정 전에는 `request_build` 뒤에 감춰 어느 쪽이든 바꿀 수 있게 한다.
- **결과 저장:** Planning의 `builds[]`는 `BuildOutcome`을 그대로 받지 않는다. 결과를 불변 기록으로 저장해 `result_ref`를 발급하고, `platform`을 `platforms[]`로 정규화해 넘긴다(저장 위치는 D-02).
- **시간 상한:** 외부 명령마다 600초가 기본이고 전체 상한은 없다. Activity timeout은 D-12 답변으로 정한다.
- **재사용은 호출 측에서:** `(resource_id, source_sha256, platform) → BuildOutcome`을 실행 기록에 남기고, 같은 키면 Build 모듈을 부르지 않는다.
- 실패 코드 분류(B-01 구현 기준):

| 코드 | 분류 | 처리 |
| --- | --- | --- |
| `DOCKERFILE_MISSING` | 수정 대상 | Fixer가 Dockerfile 생성 → 새 묶음·새 시도 |
| `EXTERNAL_COMMAND_FAILED` | 모호(다운로드·로그인·build·push 구분 불가) | 같은 `attempt_id`로 1회 재시도 → 또 실패하면 빌드 실패로 보고 Fixer, 근거 없음 표시 |
| `SOURCE_METADATA_INVALID`, `SOURCE_DIGEST_MISMATCH`, `INVALID_ARCHIVE`, `UNSAFE_ARCHIVE`, `ARCHIVE_TOO_LARGE` | 묶음 문제 | 묶음 규칙 확인·재패키징. Fixer 대상 아님 |
| `ECR_*`, `BUILD_DIGEST_*`, `LOG_UPLOAD_*` | 인프라 | Activity 재시도 후 사람 검토 |

- Build 실패 상세(`BUILD_FAILED`/`PUSH_FAILED` 분리, 정제된 출력 일부)는 급하지 않은 요청(D-04). 그 전까지 코드 수정 시연은 런타임 실패(테스트 실패) 사례로 한다.

### 2.6 Sandbox 구성·적용·갱신 (AV-011, AV-016)

호출부(Sandbox 워커)가 정하는 값:

| 값 | 규칙 |
| --- | --- |
| Namespace | `cmp-sbx-<run_id>` |
| Release 이름 | `<namespace>-<resource_id>` |
| StorageClass | capabilities의 사용 가능 목록에서 기본값(없으면 첫 항목) |
| DB 비밀번호 Secret | `<namespace>-<db id>-auth`, 무작위 생성. 재시도·갱신 때 다시 만들지 않음 |
| 앱 env Secret | `<namespace>-<app id>-env`. `.env`에서 예외 목록의 제외 변수와 DB 연결 변수를 뺀 나머지, 대체 변수는 대체값으로 |
| `env_bindings` | DB 변수는 `{resource_id, output}`, 나머지는 `{secret_ref: {name, key}}` |
| DB 이미지 | 대상 구성 단계(D-11)가 정한 Digest. 그 전까지 개발은 공식 `postgres:16` Digest 고정값 |
| NetworkPolicy | §2.7 |

`SharedDeploymentRequestV2`(`environment: SANDBOX`)를 만들어 `render_shared_deployment_v2(payload, database_chart_dir=infra/helm/postgres, application_chart_dir=infra/helm/application)`로 렌더하고, 반환된 `SharedDeploymentResultV2`의 Values로 `helm upgrade --install --wait`을 실행한다. DB 리소스의 `database.password_secret_ref`와 앱의 `{secret_ref: {name, key}}`는 워커가 미리 만든 Secret을 가리킨다.

갱신 규칙:

| 바뀐 것 | 동작 |
| --- | --- |
| 이미지 Digest만 | 앱 Release만 `helm upgrade` (롤링) |
| 앱 설정·바인딩 | 앱 Values 재렌더 → `helm upgrade` |
| env Secret 내용만 | Secret 갱신 + 앱 Pod 재시작 보장(차트의 내용 해시 어노테이션이 없으면 `kubectl rollout restart`) |
| DB 설정(바꿀 수 있는 값) | DB Release `helm upgrade` |
| 바꿀 수 없는 필드, DB 메이저 버전, 손상 | 해당 Release 삭제 + PVC 삭제 → 재설치 |
| 테스트 데이터 | 매 시도 `DROP DATABASE`·재생성 또는 합성 데이터 재주입 |

- 준비 검사: Pod Ready, `SELECT version()`으로 요구 버전 확인, SQL 접속, 앱 헬스. 공통 준비 검사 함수가 생기면 교체한다.
- DB Chart의 PVC는 `helm.sh/resource-policy: keep`이라 `helm uninstall`로 지워지지 않는다. 정리는 Namespace 삭제로 한다.
- 모든 리소스에 `cmp.sandbox/run-id`, `cmp.sandbox/expires-at`(epoch 초) 라벨. 주기 정리 작업이 만료된 Namespace를 지운다.
- 내부 Ingress는 현재 차트에 없어 첫 사이클에서는 Service로 직접 호출한다. Ingress 경로 검증은 차트 지원 후로 미룬다.
- 개발 환경: 별도 k3d 클러스터(`cmp-sandbox`). minikube는 쓰지 않는다.

### 2.7 통신 규칙·테스트 (AV-012, AV-013)

NetworkPolicy(Sandbox 워커 생성):
- Namespace 기본 차단(ingress·egress).
- `network_intent`의 리소스 간 항목(`destination_resource_id`) 허용(리소스 라벨 기준), DNS(kube-dns) 허용.
- 외부 진입(`AnalysisReport`의 진입 정보)은 테스트 실행기·Ingress Controller에서 앱 포트로의 접근만 허용.
- 외부 egress는 출구 프록시로만 허용. 프록시 허용 목록 = intent의 `external_host` 항목 + 매핑에서 `KEEP`인 서비스 주소.
- `MIGRATE` 서비스의 원본 주소(원본 DB, 원본 ALB 등)와 목록 밖 호스트는 차단·기록 → 판정 실패 → `SPEC`(의도 누락)으로 분류.

테스트:
- 테스트 정의: YAML 시나리오(`request` → `expect`) + 필요 시 Python 검사. 각 테스트에 `READ_ONLY` / `WRITES_DATA`, 외부 부수효과 여부를 표시한다. 외부 부수효과가 있는 테스트는 기본 제외.
- 묶음: 정의 + 메타데이터를 tar.gz로 묶어 D-02의 해시 이름 저장소(`ArtifactStore`, 후속에 `s3://<버킷>/<migration_id>/tests/<sha256>.tar.gz`). 이 SHA-256이 `test_suite_ref`이자 고정 해시다.
- `run_suite(suite_ref, base_url, auth_ref, include=READ_ONLY|ALL)`: 실행 전에 해시를 확인하고 결과를 구조화해 반환한다. Production에서는 민서님 쪽이 이 함수를 호출한다.
- 표준 테스트(첫 사이클): 헬스체크, 앱 API로 DB 쓰기 → 읽기 → 내용 일치, 재시작 후 데이터 유지(PVC).
- AI 테스트 검증(`validate_tests`):
  1. 원본 쪽 테스트 자원에서 통과(프로덕션 원본에서 실행 금지).
  2. 상태 코드가 아닌 결과 값 assertion이 있는지 정적 검사.
  3. 대상 의존성(DB Service, 외부 호스트)을 NetworkPolicy·프록시로 끊고 실행 → 실패해야 함.
  4. 통과한 묶음을 SHA-256으로 고정. 이후 Fixer 입력에는 읽기 전용으로만 제공하고, 실행 시 해시를 다시 확인한다.
- 실행 커버리지: Python 앱이면 `coverage run`으로 감싼 테스트 이미지 변형이 필요해 **첫 사이클 제외**, 호출 지점별 테스트 대응 표시로 대체.

### 2.8 판정과 실패 분류 (AV-014, AV-015)

- `judge`는 결정적 함수: 필수 테스트 결과, 앱 로그의 미지원 호출 패턴, 프록시 차단 기록, 매핑 대조 → `Verdict{passed, reasons[]}`.
- `classify_failure` 순서: (0) Sandbox 환경 자체가 실패했으면 `INFRA`: 클러스터 API 접근 불가, 노드 NotReady, 레지스트리 접근·인증 실패로 이미지를 못 받음, Helm·Kubernetes API 시간 초과인데 앱 Pod 이벤트·로그에 앱 원인이 없음. 앱이나 요구사항을 고쳐서 풀리는 문제가 아니므로 Fixer를 부르지 않는다 (1) 렌더 결과가 요구사항과 필드 단위로 다르면 `RENDER` (2) 빌드 실패면 `BUILD` (3) 프록시 차단이 있으면 `SPEC`(의도 누락) (4) 테스트 실패 + 스택트레이스가 앱 코드면 `CODE` (5) 나머지 `SPEC`. 애매하면 Fixer가 원인 후보를 제안하고 다음 시도의 결과로 확인한다.

### 2.9 에이전트 (Mapper·Tester·Fixer)

| 역할 | 세션 | 도구 | 금지 |
| --- | --- | --- | --- |
| Mapper | A | 증거 조회, 카탈로그·Skill 읽기 | 비밀 값, 원본 쓰기 |
| Tester | B(분리) | 사용 프로필·API 목록 읽기 | Fixer 수정 내용 보기 |
| Fixer | A | 작업 공간 diff 제안, 로그·보고서 읽기, 요구사항·intent 패치 제안 | 테스트 수정, Production 자격 증명 |

- 에이전트끼리 대화하지 않는다. Workflow가 결과물만 넘긴다.
- 모든 출력은 Pydantic 스키마로 검증한다. 판단 과정(본 증거, 시도한 수정)은 보고서에 남겨 승인 화면에 제공한다.
- LLM 클라이언트는 `cmp_agents.llm` 하나로 감싸고 공급자는 ADR로 정한다. 테스트에서는 고정 응답 가짜 클라이언트를 쓴다.

### 2.10 코드 색인과 조회 도구 (후순위, 티켓 17)

첫 사이클 이후, 스캐너 결과를 코드 색인(심볼·호출 그래프·환경변수 읽기·라우트)으로 저장하고 에이전트가 `find_calls`·`callers_of`·`env_readers`·`routes`·`snippet`·`resource_graph`로 조회하게 한다. 파일 단위 입력 대신 근거 주변 조각만 LLM에 보내고, 도구가 돌려준 위치를 그대로 `Evidence.locator`로 쓴다. 파일 해시별 캐시로 Fixer 수정 후 바뀐 파일만 다시 파싱한다. 벡터 검색은 쓰지 않는다. 분석 내부 구현이라 외부 계약은 바뀌지 않는다.

### 2.11 에이전트 실행 기반과 Skill

**실행 방식 (`cmp_agents.runtime`)**
- 에이전트는 Temporal을 모르는 함수다: `run_agent(role, task, tools, skills, budget) -> 결과 모델`. Activity는 입력을 모아 이 함수를 부르고 결과를 저장만 한다.
- 내부는 LLM 도구 호출 루프다. 매 턴 모델이 도구를 부르면 실행해 결과를 돌려주고, 모델이 **제출 도구**(`submit_mapping`, `submit_tests`, `submit_patch`)를 부르면 끝난다. 제출 인자를 Pydantic으로 검증하고, 실패하면 오류를 돌려줘 같은 루프 안에서 고치게 한다.
- 예산: 최대 턴 수, 입력·출력 토큰 상한, 벽시계 시간. 넘으면 지금까지의 기록과 함께 실패를 반환한다(Activity 재시도는 2회까지).
- LLM 클라이언트는 `cmp_agents.llm.LLMClient` 인터페이스 하나로 감싼다. 공급자·모델은 D-01로 결정됨(Claude, Anthropic SDK Messages API, `claude-sonnet-5-5`; ADR-004, `cmp_agents.anthropic_client`와 비용 장부 `cmp_agents.cost`). 테스트는 `FakeLLMClient`(정해진 도구 호출 순서를 재생)로 한다.
- 재현·감사: 각 실행의 프롬프트 버전, 불러온 Skill 목록, 도구 호출과 결과 요약, 제출 결과를 실행 기록으로 저장한다. 승인 화면과 실패 분석에 쓴다. 비밀·`.env` 값은 들어가지 않는다(입력은 마스킹본).
- 프롬프트는 `packages/agents/src/cmp_agents/prompts/<role>.md`에 두고 버전을 실행 기록에 남긴다.

**도구 (`cmp_agents.tools`)**

| 도구 | 쓰는 역할 | 내용 |
| --- | --- | --- |
| `get_evidence`, `list_call_sites`, `get_snippet` | Mapper·Fixer | 스캔 결과 조회(마스킹본). 17번 이후 코드 색인 조회로 확장 |
| `get_catalog_entry`, `load_skill` | 전 역할 | 카탈로그 항목, Skill 지침·예시 읽기 |
| `get_usage_profile`, `list_routes` | Tester | 사용 프로필·API 목록 |
| `get_attempt_report` | Fixer | 실패한 시도의 판정 이유·테스트 결과·로그 요약 |
| `propose_requirements_patch` | Fixer | `RequirementsV2`·intent 수정안(검증 후 적용) |
| `propose_code_patch` | Fixer | 작업 공간 diff. 테스트 묶음 경로·`.env`·인증 파일은 거부 |
| `submit_*` | 역할별 | 최종 결과 제출(스키마 강제) |

도구는 허용 목록으로만 등록한다. 셸 실행, 네트워크, 클라우드 자격 증명 도구는 없다.

**Skill 구조 (`skills/`)**

```text
skills/<name>/
  SKILL.md      # 언제 쓰는지, 조사 절차, 판단 기준, 대상별 차이, 수정 패턴, 테스트 패턴
  scripts/      # 결정적 보조 도구(예: Ingress 어노테이션 추출, boto3 호출 분류)
  examples/     # 원본 → 대상 변환 예시(입력·기대 출력)
```

- 카탈로그(`catalog/*.yaml`)는 **사실**: 사용 방식 어휘, 탐지 규칙, 등급 기준, 알려진 차이, 표준 테스트, 그리고 해당 Skill 이름. Skill은 **방법**이다.
- 로딩은 필요한 것만: 매핑 대상 capability의 카탈로그 항목이 가리키는 Skill의 `SKILL.md`만 컨텍스트에 넣고, `examples/`는 `load_skill(name, part="examples")`로 필요할 때 읽는다.
- `scripts/`는 에이전트가 직접 실행하지 않는다. 스캐너·검증 단계가 결정적으로 실행하고, 결과를 도구로 조회한다.
- 첫 사이클 Skill:

| Skill | 다루는 것 |
| --- | --- |
| `k8s-workload` | Deployment 자원·probe·replica, ServiceAccount·IRSA, StorageClass, 사이드카·initContainer 판단 |
| `db-postgres` | 버전·확장·연결 변수, 세션 기능(advisory lock, LISTEN/NOTIFY), 마이그레이션 실행 방식 |
| `network` | SG·Ingress·ALB·CloudFront → `network_intent`·외부 진입, 외부 호출의 `MIGRATE`/`KEEP` 판단 |
| `config-env` | `.env` 대조, 원본 환경에 묶인 변수의 제외·대체, 비밀 분류 |

고도화: `storage-s3`(엔드포인트·인증 변경과 코드 수정 패턴).

**평가**
- 정답을 아는 fixture 앱(감싼 호출, 동적 설정, 누락 변수 포함)으로 Mapper 결과를 채점한다. 실제 LLM은 D-01 이후, 사용자 승인 하에 실행한다.

## 3. 계약 초안

```python
class Evidence(Contract):
    kind: Literal["CODE", "MANIFEST", "CLOUD_RESOURCE", "IAM_POLICY", "CLOUDTRAIL", "CONFIG"]
    locator: str  # 파일:줄, 매니페스트 경로, 리소스 ID, 이벤트 ID
    verified: bool = False  # check_evidence 결과


class MappingRow(Contract):
    id: str  # 분석 버전 안에서 고유한 행 ID
    capability: str
    usage: str
    grade: Literal["DIRECT", "API_DIFF", "PARTIAL", "NONE"]
    required_proposed: bool  # Mapper 제안
    required_reason: str  # 제안 근거 요약
    required: bool | None  # 사용자 확정 전에는 None
    disposition: Literal["MIGRATE", "KEEP"]
    loses: list[str]
    treatment: Literal["PROCEED", "APPROVAL", "BLOCK"] | None  # 확정 전에는 None
    status: Literal["UNVERIFIED", "EVIDENCE_CHECKED", "SANDBOX_VERIFIED"]
    evidence: list[Evidence]


class ReviewRecord(Contract):  # AV-022 확정·승인 기록
    analysis_ref: VersionRef
    required: dict[str, bool]  # 행 ID → 확정된 필수 여부
    operational: list[str]  # 확정된 운영 요구사항(HA·백업 등)
    approvals: dict[str, Literal["APPROVED", "REJECTED"]]  # 행 ID → 승인 결정
    actor: str
    decided_at: datetime


class EnvExceptions(Contract):  # mapping_ref가 가리키는 내용
    excluded: list[str]  # 원본 환경에 묶여 빼는 변수
    replaced: dict[str, str]  # 대체 값이 비밀이 아닌 변수만
    missing: list[str]  # 코드가 읽지만 .env에 없는 변수


class ExternalEntry(Contract):  # v2 network_intent에 자리가 없는 외부 진입(D-15)
    protocol: Literal["HTTP", "HTTPS"]
    port: int
    target_resource_id: ResourceId
    target_port: int
    source_service: str  # 예: ALB, CloudFront (정보)


class AnalysisReport(Contract):  # Provisioning v1 AnalysisResult와 별개
    migration_id: str
    ref: VersionRef  # Sandbox 통과 시점 버전
    base_commit: str
    commit_verified: bool
    usage_profile: UsageProfile
    mapping: list[MappingRow]
    precheck: PrecheckReport
    external_entries: list[ExternalEntry]
    source_cidrs: list[str]  # 정보용
    source_db_images: dict[ResourceId, str]  # 원본 DB 이미지 Digest(정보, D-11 참고용)
    unmapped_items: list[str]
    unverified_risks: list[str]


class SandboxVerification(Contract):  # 내부 기록. Planning에는 SandboxEvidenceV2로 투영
    migration_id: str
    ref: VersionRef
    analysis_ref: VersionRef  # 검증한 분석 결과 버전
    target_selection_ref: VersionRef  # 검증한 대상 선택 버전
    status: Literal["PASSED", "FAILED", "NEEDS_REVIEW", "STOPPED_RENDER"]
    validated_workloads: list[ValidatedWorkloadV2]  # provisioning_v2 모델 재사용
    requirements_ref: ContentRef
    report_ref: ContentRef
    test_suite_ref: S3ArchiveRef  # provisioning_v2 모델 재사용
    attempts: list[SandboxAttemptSummary]
    cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]
    config_artifact_ref: ContentRef | None  # 검증한 별도 설정 산출물
```

- `ResourceId`, `S3ArchiveRef`, `ValidatedWorkloadV2`, `RequirementsV2`는 `cmp_contracts.provisioning_v2`에서 import한다.
- `AnalysisReport.mapping`의 행 ID는 해당 분석 버전 안에서 중복될 수 없다. `ReviewRecord`의 행 ID와 `analysis_ref`가 함께 검토 대상을 식별한다.
- `ReviewRecord` 단독 검증은 참조된 분석 결과의 행 존재 여부를 확인하지 않는다. 08B 검토 게이트가 분석 버전을 읽어 행 ID와 승인 대상을 대조한다.
- Planning 입력 v2(`PlanningRequestV2`)로의 투영: `analysis.result_ref` ← `AnalysisReport.ref`, `analysis.requirements_ref` ← 요구사항 JSON 바이트 참조(SHA-256), `analysis.source_refs` ← APPLICATION ID별 최종 소스 아카이브, `builds[]` ← 저장된 Build 결과(`platforms[]`로 정규화), `sandbox` ← `SandboxVerification`(`PASSED`일 때만, 내부 상태 `NEEDS_REVIEW`·`STOPPED_RENDER`는 넘기지 않음), `environment.mapping_ref` ← `EnvExceptions` 참조. 대조는 `prepare_plan_v2`(민서)가 한다.
- 내부 Sandbox 기록에 검증한 분석·대상 선택 버전을 보관하고 투영 시 현재 분석·대상 선택과 대조한다(AV-018). 요구사항·설정 참조는 분석 입력과 Sandbox 기록을 각각 전달해 Planning에서 대조하며 한쪽 값으로 다른 증거를 채우지 않는다.
- `project_planning`은 요구사항 참조·S3 아카이브·대상 선택·환경·정책에 기존 v2 및 공통 참조 모델을, Build에는 `BuildAttempt` 모델을 입력받는다. 소스는 리소스 ID별 S3 아카이브 모델의 매핑이다.
- `BuildAttempt`는 Build 결과의 migration·resource·attempt ID, 불변 결과 참조, 소스·플랫폼·이미지·로그 참조와 오류 코드를 기록한다. 투영 시 migration ID를 대조하고 단일 `platform`을 Planning의 `platforms` 목록으로 바꾼다. 실제 결과 저장 위치는 D-02로 남긴다.
- 01의 초기 `UsageProfile.usages`와 `PrecheckReport.findings`는 문자열 목록이다. `AnalysisRequest`는 저장소·애플리케이션 식별자, `.env`·원본 접근 참조와 v2 대상 선택을 받는다. `SourceArchive`는 기존 `S3ArchiveRef`를 재사용한다. 요구사항 JSON은 UTF-8, 정렬된 객체 키, 공백 없는 구분자로 직렬화하며 배열 순서는 유지한다. 상세 스캔 결과의 구조화는 후속 티켓에서 다룬다.

## 4. Activity 경계와 실행 설정

| Activity | Queue | timeout | 재시도 | 비고 |
| --- | --- | --- | --- | --- |
| `fetch_source` | analysis | 2분 | 3(네트워크만) | 같은 커밋 재사용 |
| `scan_*` | analysis | 3분 | 3(스로틀링만) | Heartbeat. 권한 거부는 비재시도 오류 |
| `build_requirements`, `precheck`, `check_evidence` | analysis | 1분 | 2 | 결정적 |
| `map_usage`, `generate_tests`, `fix_*` | analysis / sandbox | 2분 | 2 | 스키마 검증 실패도 재시도 |
| `validate_tests` | sandbox | 5분 | 1 | 원본 테스트 자원 필요 |
| `package_source` | sandbox | 2분 | 2 | 같은 해시면 업로드 생략 |
| `request_build` | sandbox (D-12) | D-12 | 1(+분류 규칙) | Heartbeat. 실패는 결과값으로 반환 |
| `prepare_sandbox` | sandbox | 2분 | 2 | Secret·NetworkPolicy 생성, 기존 Secret 있으면 재사용 |
| `render_sandbox`, `apply_sandbox` | sandbox | 5분 | 2 | `helm upgrade --install --wait`. 실제 구현은 `sbx_*` Activity(`sbx_build`·`sbx_prepare`·`sbx_apply`·`sbx_verify`·`sbx_classify`·`sbx_fix`·`sbx_plan_publish`·`sbx_publish`·`sbx_teardown`, `activities/sandbox_loop.py`)이며 이 표의 이름은 설계 시점의 역할 구분이다 |
| `readiness_check`, `reset_test_data` | sandbox | 2분 | 2 | |
| `run_tests` | sandbox | 3분 | 1 | 테스트 실패는 결과값 |
| `judge`, `classify_failure` | sandbox | 30초 | 2 | 결정적 |
| `compute_treatment`, `record_review` | analysis | 30초 | 3 | 결정적. 사용자 응답 대기는 Workflow의 Signal 대기(Activity 아님) |
| `publish_final_report` | sandbox | 1분 | 3 | 불변 저장, 같은 내용이면 같은 참조 |
| `teardown_sandbox` | sandbox | 3분 | 5 | 취소·실패에도 실행, TTL 이중 정리 |

수치는 측정 후 조정한다. 비재시도 오류는 `ApplicationError(non_retryable=True)`로 구분한다.

## 5. Failure, retry, and rollback behavior

| 실패 | 처리 |
| --- | --- |
| 클론 실패·권한 없음 | 비재시도, 사용자 조치 요청 |
| 스캔 권한 거부 | 비재시도, 필요한 권한 이름 반환 |
| LLM 출력 스키마 오류 | 2회 재시도 후 사람 검토 |
| 근거 확인 실패 | 해당 행 `UNVERIFIED`, 진행은 처리 규칙에 따름 |
| 필수 기능 대응 없음 | 분석 단계에서 차단, Sandbox로 가지 않음 |
| 사용자 확정·승인 미응답 | 대기 유지(타이머 상한 후에도 자동 승인 없음), Query로 대기 상태 표시 |
| 다른 분석 버전에 대한 승인 Signal | 거절, 현재 버전으로 다시 요청 |
| 에이전트 예산 초과·스키마 실패 반복 | 실행 기록과 함께 실패, 사람 검토 |
| 확장 요구가 있는 앱 | 분석에서 `BLOCK` 보고(Planning이 거절하므로). D-14 이후 재검토 |
| 빌드 실패 | §2.5 분류 |
| Helm 적용 실패 | Release 상태 조회 → 같은 Values로 재시도, 바꿀 수 없는 필드면 재생성 분기 |
| 준비 검사 실패 | 테스트 없이 실패 판정, 원인 분류로 |
| 테스트 실패·출구 차단 | 결과값, 수정 루프 |
| 렌더 결과 불일치 | 루프 중단, Provisioning 담당 보고 |
| 3회 초과 | 사람 검토 Signal 대기(타이머 상한), 정리 실행 |
| Worker 장애·취소 | `finally` 정리 + TTL 라벨 정리 작업 |

Production 롤백은 이 기능 범위가 아니다. Sandbox는 정리가 곧 원상 복구다.

## 6. 보안

- 마스킹(`cmp_agents.masking`): 고엔트로피 문자열, 알려진 키 패턴(AKIA…, `-----BEGIN`), `.env` 값 위치. 마스킹본만 LLM에 보낸다.
- `.env` 값은 `prepare_sandbox`에서 Secret을 만들 때만 읽는다. Activity 인자·결과·로그에 싣지 않고, 참조(`env_file_ref`)만 넘긴다.
- 외부 부수효과가 있는 테스트는 기본 제외한다(실제 `.env` 키로 메일·결제가 일어나지 않게).
- Sandbox Worker 권한: Sandbox 클러스터의 지정 Namespace 패턴만, 소스·테스트 묶음 S3 쓰기, (D-12에 따라) Builder 실행. Production 자격 증명 없음.
- 로그에는 참조·해시·오류 코드만 남긴다. Helm·Docker 원시 출력은 공개 오류에 복사하지 않는다(민서 03과 같은 원칙).

## 7. Verification

| 대상 | 방법 | 증명하지 못하는 것 |
| --- | --- | --- |
| 계약 | Pydantic 모델 테스트, fixture JSON 왕복, Planning v2 투영 검사 | 상대 Worker의 실제 출력 |
| 스캐너 | 데모 앱 축소 fixture(코드·매니페스트) 결과 스냅샷, AWS는 `botocore.stub` | 실제 계정의 권한·지연 |
| 매핑 | 가짜 LLM 응답으로 스키마·처리 표·근거 확인 검사 | 실제 모델의 판단 품질 |
| 소스 묶음 | 같은 입력 두 번 → 같은 해시, 제외 규칙, B-01 아카이브 검사 통과 | 실제 S3 권한 |
| Sandbox | 로컬 k3d + 실제 Helm, 포트 오류·intent 누락 주입 → 수정 → 통과 | GKE/EKS Sandbox 차이 |
| Workflow | Temporal 테스트 환경(time skipping)으로 루프·3회 초과·취소 정리, `make smoke-temporal` | 실제 외부 시간 |

`make check`는 Python 변경마다, HTTP 변경 시 `make openapi`.

## 8. 요구사항 대응표

| 요구사항 | 설계 위치 | 티켓 |
| --- | --- | --- |
| AV-001 | §1, §3 | 01, 02, 18 |
| AV-002 | §2.1 | 03, 18 |
| AV-003 | §2.2, §2.10 | 05, 17 |
| AV-004 | §2.2 | 04 |
| AV-005 | §2.2 | 06 |
| AV-006 | §2.2 | 07 |
| AV-007, AV-008 | §2.3, §2.9, §2.10, §2.11 | 08A, 08, 17 |
| AV-009 | §2.4 | 09 |
| AV-010 | §2.5 | 10 |
| AV-011 | §2.6 | 11, 11B |
| AV-012 | §2.7 | 12 |
| AV-013 | §2.7 | 13 |
| AV-014, AV-015 | §2.8 | 02B, 13, 14 |
| AV-016 | §2.6, §5 | 02B, 11 |
| AV-017 | §3 | 01, 07, 14 |
| AV-018 | §3 | 15 |
| AV-019 | §2.1 | 14 |
| AV-020 | §6 | 전 티켓 |
| AV-021 | §4, §5 | Q-01, 02, 02B, 전 티켓 |
| AV-022 | §2.3 | 08B |

## 9. 결정 대기

| ID | 항목 | 현재 방향 | 담당 |
| --- | --- | --- | --- |
| D-01 | LLM 공급자·모델·전송 범위·비용 상한 | **결정됨** — ADR-004(Accepted). Sonnet 5.5, 마스킹한 증거 조각만 전송, 마이그레이션당 $3·하루 $20·한 달 $100 기본 상한을 코드로 강제 | 승철·팀 |
| D-02 | `*_ref` 저장 위치, Build 결과 저장·정규화 Activity 담당 | 첫 사이클은 해시 이름 로컬 불변 저장소로 **결정됨**. Planning 입력 v2의 `S3ArchiveRef`(소스·테스트 묶음)는 S3 URI를 요구하므로 S3 연결 방식이 남음(15번 전) | 민서·승철 |
| D-04 | Build 실패 상세 | 급하지 않음 | 민서 |
| D-06 | 소스·테스트 묶음 버킷 소유·정리 규칙 | 플랫폼 소유 버킷, 실행 종료 후 N일 | 민서·승철 |
| D-07 | 첫 사이클 지원 언어 | 승철이 결정(데모 앱은 그 언어에 맞춤) | 승철 |
| D-08 | Sandbox 기반 클러스터 준비 주체·시점 | **결정됨** — 로컬 k3d `cmp-sandbox`(Colima) | 승철 |
| D-09 | AI 테스트 1번 조건용 원본 쪽 테스트 자원 | 미해결. 없으면 `validate_tests`가 `ORIGIN_UNAVAILABLE`로 AI 테스트를 모두 거절하므로 표준 테스트만 쓴다 | 승철·소울 |
| D-10 | Production 테스트 묶음 실행 시점·데이터 정리 | 데이터 이전 후·트래픽 전환 전 | 민서·소울·상헌 |
| D-11 | DB 이미지를 고르는 "대상 구성 단계"의 소유자, Sandbox·Production 공유 방식 | 요구사항엔 이미지 없음(#22). 원본 이미지는 분석 결과에 정보로 | 민서 |
| D-12 | Build 호출 Worker(권한 배치)·전체 시간 상한 | `resource_id`·`source_version_id`는 구현됨. 함수 호출 유지 | 민서 |
| D-13 | DB 내부 상태 사전 점검 담당(기본 키·시퀀스·복제 설정) | Data Migration 쪽 제안 | 소울 |
| D-14 | PostgreSQL 확장 지원 시점과 `CREATE EXTENSION` 실행 주체 | 현재 확장 요구는 Planning이 거절 | 민서 |
| D-15 | 외부 진입(인터넷 → 앱)을 `network_intent`로 표현하는 방법 | 그 전까지 `AnalysisReport.external_entries`에 기록 | 민서 |
