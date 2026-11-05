# ENV-02 — 시작 요청 env_file_ref와 Workflow 전달

## 담당
민서 (#60 담당과 조율)

## 상태
IN_PROGRESS — [#146](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/146). 구현·테스트 완료, 조정 세션 검토 대기(push·PR 전).

## 착수 결정 (2026-11-05, 사용자 승인)
- ENV-01과 같은 저장 위치·보관 결정을 따른다. 시작 요청이 받아들여지면 `bind_migration`으로 파일과 이전을 묶어 보관 기간이 그 이전 종료에서 시작한다.
- #60은 SDD만 병합됐고 구현 PR이 없다(2026-11-05 확인). 그래서 이 티켓이 시작 입력 형식을 정했다: 기존 dict 입력(`{project_id, analysis, sandbox, planning?}`)에 선택 키 `env_file_ref`를 더하고, API scaffold 시작은 참조가 있을 때만 `{project_id, env_file_ref}` dict로 보낸다(없으면 지금처럼 문자열). `analysis` 키가 있는 dict만 실제 파이프라인 입력이다. #60은 이 dict에 소스 필드를 더하면 된다. patch 이름은 `env-file-ref-v1`(이 티켓 전용).
- 계약 추가: `AnalysisInput.env_file_ref: ContentRef | None`(선택, 분석 로직 변경 없음 — 박승철님 공유 필요), `SandboxSettings`·`SandboxInput.env_file_ref`(선택, 통과만), `MigrationWorkflow.status`의 `env_file_ref`. Planning은 기존 `PlanningSettings.env_file_ref`를 시작 요청 값으로 덮어쓴다.
- 존재 확인은 복호화 없이 메타의 `version`·`sha256`만 본다(`check`).

## 목적
이전 시작 요청이 `.env` 참조를 받아, 분석·샌드박스·구축이 같은 참조를 쓰게 한다.

## 관련 요구사항
ENV-004. PROV-004.

## 선행 작업
ENV-01(참조 형식). #60과 같은 요청 모델·Workflow 입력을 바꾸므로 순서를 맞춘다.

## 현재
`MigrationRequest{project_id, source_provider, target_provider}`. `POST /migrations`는 `project_id`만 Workflow에 넘긴다. `AnalysisInput`에 env 필드가 없다.

## 범위
- 포함
  - `MigrationRequest.env_file_ref: ContentRef | None`(선택). 받으면 ENV-01 저장소에 존재·버전·`sha256`이 맞는지 확인하고, 아니면 422 `ENV_FILE_NOT_FOUND`·`ENV_FILE_DIGEST_MISMATCH`.
  - Migration Workflow 입력에 참조를 싣고, 분석(`AnalysisInput.env_file_ref`), 샌드박스, 구축(Planning 입력 v2의 `environment.env_file_ref`)으로 전달한다. 값은 싣지 않는다.
  - 기존 Workflow 이력 재생을 위해 `workflow.patched` 게이트를 쓴다.
- 제외: 분석·샌드박스 내부에서 참조를 쓰는 코드(ENV-05·06), 연결 ID 필드(별도 후속)

## #60과 겹치는 부분
- 둘 다 `MigrationRequest`에 선택 필드를 더하고, Workflow 시작 인자를 문자열(`project_id`)에서 입력 모델로 바꿔야 한다.
- 먼저 병합되는 쪽이 "시작 입력 모델"을 만들고, 다른 쪽은 필드만 더한다. 두 작업이 같은 patch 이름을 쓰지 않게 한다.

## 수용 기준
- [x] `env_file_ref`가 없는 기존 요청은 그대로 동작한다.
- [x] 받은 참조가 분석·샌드박스·구축 입력에 같은 값으로 전달된다(실제 파이프라인 입력 기준. API scaffold 시작은 분석·Sandbox가 scaffold 문자열 입력이라 Workflow 입력·`status`에만 실린다).
- [x] Temporal 입력·이력에는 참조(`id`, `version`, `sha256`)만 있다.
- [x] 기존 Workflow 이력이 Replayer로 재생된다.

## 구현 기록
- 코드: `cmp_contracts.migration.MigrationRequest.env_file_ref`, `cmp_api.routes.migrations`, `cmp_worker.workflows.migration`(`ENV_FILE_REF_PATCH`), `cmp_worker.sandbox_input`, `cmp_worker.activities.sandbox_loop.SandboxInput`, `cmp_worker.start_input`·`scripts/start_migration.py --env-file-ref`.
- 테스트: `tests/test_env_files.py`(시작 요청 422·참조만 전달), `tests/analysis/test_pipeline_wiring.py`(time-skipping으로 분석·Sandbox·Planning 인계 입력 확인, 새 이력 Replayer, 참조 없을 때 marker 없음, API scaffold dict 시작). 기존 고정 이력 재생 테스트(`tests/analysis/test_workflow.py`, `tests/test_migration_plan_workflow.py` 등) 통과.
- 검증하지 않은 것: 로컬 Temporal 서버 `make smoke-temporal`.

## Test Seam
Temporal time-skipping 테스트 + Replayer, API TestClient.
