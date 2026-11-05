# ENV-02 — 시작 요청 env_file_ref와 Workflow 전달

## 담당
민서 (#60 담당과 조율)

## 상태
DRAFT

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
- [ ] `env_file_ref`가 없는 기존 요청은 그대로 동작한다.
- [ ] 받은 참조가 분석·샌드박스·구축 입력에 같은 값으로 전달된다.
- [ ] Temporal 입력·이력에는 참조(`id`, `version`, `sha256`)만 있다.
- [ ] 기존 Workflow 이력이 Replayer로 재생된다.

## Test Seam
Temporal time-skipping 테스트 + Replayer, API TestClient.
