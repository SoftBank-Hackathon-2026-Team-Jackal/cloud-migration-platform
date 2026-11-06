# Ticket 44 — 웹 시작 요청을 실제 실행 입력(PlanningSettings)으로 연결

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 조율 세션 리뷰·병합 대기) — [Issue #179](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/179). 2026-11-06 조율 세션 지시로 착수(2차 병렬 묶음 44·46·52·50·54). #162(38)와 같은 요청 모델.

## 배경 (실동작 점검 A-01·A-02·A-11, D-03, 흐름 점검 N4)
- A-01: 웹 `POST /migrations`의 `source`·`target`이 `MigrationRequest`에 없어 오류 없이 버려진다(pydantic 기본 `extra=ignore`). `target_provider=ON_PREM`과 문자열 `env_file_ref`는 422다.
- A-02: Planning 인계(`planning_handoff.assemble_registration`)는 `PlanningSettings`(대상 선택·대상 설정·`env_file_ref`)가 있어야 도는데, 이 값을 만드는 곳이 CLI `scripts/start_migration.py --planning` JSON뿐이다.
- A-11: `.env` 없는 이전은 오류 코드 없이 실패한다.
- D-03 실측(49 병합 후): 웹과 같은 요청으로 시작하면 `plan_failed`/`PLANNING_INPUT_MISSING`.

## 관련 요구사항
PROV-001, PROV-002, PROV-004, PROV-021. 정책: [제품 정책](../../docs/product/policies.md) `.env` 별도 업로드. 계약: 웹 `cloud-migration-web` `contract/rest.schema.json` `StartRequest`(읽기만, `a9bc17e`).

## 선행 작업
Blocked by: 18, 22, 23, ENV-02, 49 (main 반영). 동시 진행: 46(#181, `credential_ref` 해석), 50(#185, 실행 뒤 인계), 52, 54. 다음 차례: 55(#190, EnvPlan).

## 작업 범위
- 포함
  - 웹 `StartRequest`와 같은 시작 요청 모델(`target`·`source`·문자열 `env_file_ref`, ON_PREM·AWS·GCP), 모르는 필드 422
  - 대상 선택 → `PlanningSettings`(대상 선택 `TargetSelectionV2` + 대상 설정 `TargetSettings`) 조립, 실제 파이프라인 시작 입력(`analysis`·`planning`·`env_file_ref`)으로 시작
  - 대상 연결 ID → 대상 설정 `credential_ref`(`VersionRef`) 채우기, ON_PREM Agent ID → `cluster_uid` 채우기
  - `.env` 필수 정책과 시작 시점 오류 코드
- 제외
  - `credential_ref`를 실행 프로필로 해석(46), CIDR 겹침 검사(#157), DB 이미지 대조(#155), EnvPlan(55), 실행 뒤 인계(50)
  - 원본 연결 ID → 분석 `credential_ref`·원본 EKS `cluster_name`(#162에 남김, 아래)
  - 데모 worker 분석·샌드박스 설정(A-03/D-08), 웹 저장소 변경(협의 필요)

## 착수 결정 (2026-11-06)

| ID | 결정 | 근거 |
| --- | --- | --- |
| S1 요청 모양 | `MigrationRequest`는 웹 `StartRequest` + 기존 필드다. `project_id`(필수), `source_provider`(`AWS`), `target_provider`(`AWS`·`GCP`·`ON_PREM`, 선택), `target{provider, profile, region?, agent_id?, credential_ref?}`, `source{kind: GITHUB_PUBLIC, repository_id, full_name, commit_sha}`, `env_file_ref`(문자열 또는 `ContentRef` 객체). 웹 스키마의 `target.provider`는 GCP·ON_PREM뿐이지만 플랫폼은 AWS도 받는다(상위 집합) | 웹 계약이 기준이다. AWS 대상(08·09)은 이미 Plan 경로가 있다 |
| S2 모르는 필드 | 요청·`target`·`source` 모두 `extra="forbid"`, 모르는 필드는 422다 | A-01의 "오류 없이 버림"을 막는다. 웹 스키마도 `additionalProperties: false`다 |
| S3 두 경로 | `target`이 없으면 기존 scaffold 시작 그대로(입력 바이트 동일, ENV-02 동작 유지). `target`이 있으면 실제 시작이며 `source`·`env_file_ref`가 필수다(`SOURCE_REQUIRED`·`ENV_FILE_REF_REQUIRED`, 422) | 웹 스키마에서 `target`은 필수라 웹 요청은 모두 실제 시작이다. scaffold 시작은 smoke·기존 호출자용으로 남긴다. 소스 없이는 분석·Build 증거가 없어 Plan을 만들 수 없으므로 시작에서 막는다 |
| S4 `.env` 필수(A-11) | 실제 시작은 `.env` 참조가 필수다. 환경변수가 없는 앱은 빈 `.env`를 올린다. 시작 시점에 저장소에서 확인하고, 없거나 바뀌었으면 기존 ENV-02 코드(`ENV_FILE_NOT_FOUND`·`ENV_FILE_DIGEST_MISMATCH`·`ENV_FILE_EXPIRED`)로 422, 저장소 미설정은 503 | 제품 정책이 `.env` 별도 업로드이고, Planning 입력 v2 `EnvironmentInputV2.env_file_ref`가 필수다. Optional로 바꾸는 것은 01A 공유 계약 변경이라 피했다 |
| S5 `env_file_ref` 문자열 | 문자열은 `envf-<32 hex>`(업로드 응답의 `id`)다. API가 그 파일의 **최신 버전**으로 `ContentRef{id, version, sha256}`를 만들어 Workflow에 넣는다(이후 고정). 객체는 기존처럼 정확한 버전이다 | 웹은 문자열 하나만 보낸다. 시작 순간에 버전·sha를 고정해 이력에는 불변 참조만 남는다 |
| S6 대상 설정 생산(A-02) | **대상 Profile 카탈로그**(운영자 설정 JSON, `PLANNING_TARGET_PROFILES_FILE`)가 대상 설정 기본값을 정한다. 항목은 `profile` 이름별 `{provider, settings, workloads}`(AWS·GCP, 18·22 등록 모양) 또는 `{provider: ON_PREM, environment_id?, namespace, storage_class, database_image}`다. 코드에는 기본값을 두지 않는다. 카탈로그가 없으면 503 `TARGET_PROFILES_UNCONFIGURED`, 이름이 없으면 422 `TARGET_PROFILE_NOT_FOUND`, Provider가 다르면 422 `TARGET_PROFILE_PROVIDER_MISMATCH` | plan.md는 Node·네트워크 Profile 기본값을 미확정으로 두었고, 22 W3은 DB 이미지 기본값을 두지 않기로 했다. 계정·State 버킷·API 접근 CIDR은 환경마다 달라 코드 상수가 될 수 없다. 웹은 이미 `profile`(`gcp-vm-basic`·`onprem-k3s-basic`)을 보낸다 |
| S7 사용자 입력 범위 | 사용자가 바꿀 수 있는 값은 `region`(AWS·GCP, `settings.region`과 `location`), 대상 연결(`credential_ref`, AWS `account_id`·GCP `project_id`도 연결 값으로), ON_PREM `agent_id`(`cluster_uid`는 Agent 등록 값, `environment_id`는 카탈로그 값 또는 `agent_id`)뿐이다. VPC CIDR·서브넷·노드·Namespace·StorageClass·DB 이미지는 카탈로그 값 그대로다. 대상에 맞지 않는 필드(클라우드의 `agent_id`, ON_PREM의 `region`·`credential_ref`)는 422 `TARGET_FIELD_NOT_APPLICABLE`, 클라우드 리전이 어디에도 없으면 422 `TARGET_REGION_REQUIRED`. 결과 설정은 시작 시점에 `TargetSettings`와 `AwsPlanInput`·`GcpPlanInput`으로 검증하고 실패하면 422 `TARGET_SETTINGS_INVALID` | 크기·대역을 사용자가 바꾸는 것은 비용·겹침 검사(#157)와 함께 정할 정책이라 이번에는 막는다. 잘못된 설정은 분석·샌드박스를 다 돈 뒤가 아니라 시작에서 거절한다 |
| S8 대상 연결 → `credential_ref` | `target.credential_ref`는 연결 ID `conn-<hex>` 또는 그 AWS 프로필 이름 `cmp-conn-<hex>`(23 `credential_ref`)를 받는다. 연결은 `AWS_TARGET`·`GCP_TARGET`이고 대상 Provider와 같아야 하며 `CONNECTED`여야 한다(422 `TARGET_CONNECTION_NOT_FOUND`·`TARGET_CONNECTION_KIND_MISMATCH`·`TARGET_CONNECTION_NOT_READY`). 대상 설정 `credential_ref`는 `VersionRef{id, version: "1"}`이고 `id`는 AWS면 `cmp-conn-<hex>`(shared-contracts 23 해석 그대로), GCP면 `conn-<hex>`(연결 `credential_ref`가 `null`이라서)다. 연결을 주지 않으면 카탈로그 값(플랫폼 기본)을 쓴다. 이 값을 실행 프로필로 해석하는 것은 46이다 | Plan 입력 계약(`AwsPlanInput`·`GcpPlanInput.credential_ref: VersionRef`)을 바꾸지 않는다. 46이 `id`만 보고 해석할 수 있게 23의 이름을 그대로 쓴다 |
| S9 대상 선택 참조 | `TargetSelectionV2{ref: {id: "target-<profile>", version: 해석된 대상 설정 sha256 앞 16자}, provider, location: 리전(ON_PREM null), access_ref: 클라우드는 `credential_ref`, ON_PREM은 `{id: agent_id, version: cluster_uid}`, capabilities_ref: {id: "profile-<profile>", version: 카탈로그 항목 sha256 앞 16자}, profile}` | 같은 대상 설정이면 같은 ref라 샌드박스가 기록한 대상과 비교할 수 있다(A-10은 별도). 비밀값이 없다 |
| S10 시작 입력 | API가 `scripts/start_migration.py`(`build_start_input`)와 같은 dict를 만든다: `{project_id, analysis: AnalysisInput{repository: "https://github.com/<full_name>.git", requested_commit: commit_sha}, sandbox: null, planning: PlanningSettings(env_file_ref 제외), env_file_ref}`. `MigrationWorkflow._run`은 이미 이 모양을 처리하므로 **Workflow 코드는 바꾸지 않는다**(patch 추가 없음). `env_file_ref`는 ENV-02 규칙대로 부모가 `PlanningSettings`에 복사한다 | 기존 결정적 경로(15 인계, ENV-02 복사)를 그대로 쓴다. Workflow를 고치지 않아 50(`migration.py` 실행 뒤 부분)과 겹치지 않는다. API는 `cmp_worker`를 import하지 않고 `cmp_contracts`·`cmp_providers` 모델로만 검증한다 |
| S11 #162와의 경계 | 이 티켓이 #162의 `target_connection_id`(= `target.credential_ref`)·`target_agent_id`(= `target.agent_id`)와 대상 설정 연결을 처리했다. #162에 남는 것은 `source_connection_id` → 분석 `AnalysisInput.credential_ref`(`cmp-conn-<hex>`)와 원본 EKS `cluster_name`, Migration 담당 통보다 | 웹 계약 필드 이름(`target.credential_ref`·`target.agent_id`)을 따른다 |
| S12 55와의 경계 | `PlanningSettings` 조립은 `cmp_api.start_request.planning_settings`(순수 함수, 입력: 요청 `target`·카탈로그 항목·연결·Agent)다. EnvPlan은 여기 싣지 않는다. 55는 `sbx_plan_assemble` → `assemble_registration(env_plan=...)` 쪽에 싣는다 | `PlanningSettings`는 `extra="forbid"`이고 시작 시점에는 EnvPlan이 없다(분석 결과) |

## 수용 기준 (Issue #179)
- [x] 웹과 같은 요청으로 시작한 이전이 Plan 게시 단계까지 간다(로컬 통합 실행). — API가 만든 시작 입력이 CLI 시작 입력과 바이트까지 같고(Workflow 경로는 `tests/analysis/test_pipeline_wiring.py`가 이 모양으로 `sbx_plan_assemble`·등록까지 확인), 그 `planning`으로 조립한 등록이 Planning 등록을 통과하고 ON_PREM은 실제 `PlanPublisher.publish`가 `PLAN_PUBLISHED`다. 데모 compose 실측의 정지 지점은 PR 본문(분석 단계, A-03/D-08 범위 밖).
- [x] ON_PREM·AWS·GCP 대상 모두 시작 요청이 받아들여지고 버려지는 필드가 없다.

## Test Seam
- API: FastAPI `TestClient` + 가짜 Temporal 시작 + 임시 `.env` 저장소 + SQLite(연결·Agent) + 임시 카탈로그 파일. 시작된 Workflow 입력 dict를 단언한다.
- 조립 결과가 실제로 쓰이는지: API가 만든 `planning`으로 `PlanningSettings.model_validate` → `assemble_registration`(분석 fixture) → `check_with_planning`, 클라우드는 `AwsPlanInput`·`GcpPlanInput` 검증.
- Workflow: 코드를 바꾸지 않았으므로(S10) API 입력이 `build_start_input`(CLI) 결과와 같은지만 단언한다. 그 모양이 `sbx_plan_assemble`의 `settings`로 가는 것은 기존 `tests/analysis/test_pipeline_wiring.py`(time-skipping 서버)가 확인한다.
- Plan 게시: ON_PREM은 같은 테스트에서 SQLite 등록 → 실제 `PlanPublisher.publish` → `PLAN_PUBLISHED`. AWS·GCP는 `register_planning_input`까지(게시는 Terraform·클라우드 호출).
- 예시 카탈로그 `infra/demo/target-profiles.example.json`의 모든 항목이 유효한 `PlanningSettings`를 만든다.
- 점검 A 재현(`a-repro`)의 A-01 xfail 3개는 S3·S4·S6 정책(실제 시작에 `.env`·카탈로그 필요)을 반영해 `tests/test_web_start_request.py`로 옮겼다(RED → GREEN). 원본 a-repro는 정책 이전 요청이라 그대로는 422/503이고, "현재 동작"(필드 버림) 테스트는 이제 실패한다(의도대로).

## 검증
- `uv run pytest tests/test_web_start_request.py`: 33 passed. RED는 설정·의존성 부재로 30 errors(2026-11-06).
- `make check`: 통과(ruff, 전체 pytest). 옛 정책(`GCP`만 허용)을 단언하던 `tests/test_scaffold.py::test_contract_rejects_unplanned_target`을 새 계약으로 고쳤다.
- `make openapi`: `MigrationRequest`·`StartTarget`·`StartSource`와 `POST /migrations` 설명이 바뀌었다(리뷰함).
- 데모 compose 로컬 실측(2026-11-06, 점검 D `d-repro` 방식: 이 브랜치로 api·worker 이미지 빌드, 호스트 경로·포트만 바꾼 override, api에 `.env` 저장소·카탈로그만 추가, 클라우드 자격 없음, `alembic upgrade head` → `20261106_01`):
  - 웹 `sseProvider`와 같은 본문(`project_id`·`source_provider`·`target_provider` + `target`·`source`·`env_file_ref` 문자열)으로 ON_PREM(`onprem-k3s-basic`, HTTP로 등록한 Agent)·GCP(`gcp-vm-basic`, `asia-northeast3`) 모두 202.
  - Temporal 이력의 시작 입력: `analysis`(`https://github.com/<full_name>.git`, commit), `planning`(ON_PREM 대상 설정에 Agent 등록 `cluster_uid`), 최신 버전으로 고정된 `env_file_ref{id, version, sha256}`.
  - **정지 지점: 분석 자식**(약 4초, `failed`/`CHILD_WORKFLOW_FAILED`, `failure_phase=analysis`). 분석 자식 Activity 실패 메시지 "Failed decoding arguments": 데모 worker에 분석 설정(`ANALYSIS_ARTIFACT_STORE_DIR` 등)이 없어 scaffold Activity가 dict 입력을 못 받는다(A-03/D-08, 이 티켓 범위 밖). 자격 단계까지는 가지 않는다. 그 뒤 경로(인계 → 등록 → Plan 게시)는 위 단위·통합 테스트로 확인했다.
  - 거절: `env_file_ref` 없음 → 422 `ENV_FILE_REF_REQUIRED`, 모르는 필드 → 422, `{"project_id"}`만 → 202 scaffold(기존 그대로).
  - 컨테이너·볼륨·이미지·임시 데이터는 삭제했다.

## 미검증
- 데모 서버·실제 웹 BFF에서의 시작(승인 대기: 데모 서버 변경 금지)
- 실제 분석·샌드박스를 지나 Plan 게시까지(데모 worker에 분석·샌드박스 설정이 없음, A-03/D-08)
- 데모 compose api에는 `.env` 저장소 설정(`ENV_FILE_STORE_DIR`·`ENV_FILE_ENCRYPTION_KEY`)이 없다(N12). 서버에 설정하기 전까지 데모의 웹 시작은 503 `ENV_FILE_STORE_UNCONFIGURED`다. 서버 `.env` 변경이라 이 PR에 넣지 않았다(승인 대기)

## 협의 필요
- 웹(김민서, 별도 저장소): `contract/rest.schema.json` `target.provider`에 `AWS` 추가 여부, `target.credential_ref`에 연결 ID(`conn-<hex>`)를 넣는다는 의미 기록, `env_file_ref` 문자열이 `envf-<32 hex>`(업로드 `id`)라는 형식 기록, 실제 시작에 `source`·`env_file_ref`가 필요하다는 점(웹은 지금 `env_file_ref`를 보내지 않는다).
- 데모 운영: 카탈로그 파일(`infra/demo/target-profiles.example.json` 모양)을 서버 `.env`의 `PLANNING_TARGET_PROFILES_FILE`로 둔다.
- 46: S8의 `credential_ref.id` 규칙(AWS `cmp-conn-<hex>`, GCP `conn-<hex>`)을 해석 쪽에서 받는다.

## 문서 변경
- `specs/provisioning/shared-contracts.md`(시작 요청·카탈로그), `specs/provisioning/tasks.md`, `infra/demo/compose.yaml`(api `PLANNING_TARGET_PROFILES_FILE`·`/opt/cmp/config` 읽기 전용 마운트), `infra/demo/target-profiles.example.json`(예시), `specs/env-file-ref/spec.md`(문자열 참조·필수), `docs/engineering/env-file-guide.md`, `docs/contracts/openapi.yaml`(재생성), `tickets/provisioning/18`·`22`·`49`(시작 입력 출처 한 줄), `specs/analysis-verification/spec.md`·`plan.md`·`tasks.md`, `tickets/analysis-verification/15-planning-handoff.md`(설정 출처). D-15는 영향 없음(문서 변경 없음).

## 완료 체크리스트
- [x] RED → GREEN 테스트
- [x] `make check`
- [x] `make openapi` 재생성
- [x] SDD·티켓 문서 갱신
- [ ] 리뷰·병합(조율 세션)
