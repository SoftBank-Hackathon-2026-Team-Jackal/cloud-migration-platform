# Ticket 08 — 웹 실제 시작에 데이터 이전 입력 연결

## 상태

DONE(PR #254 병합). 데모 서버에 값을 넣는 일과 worker의 클러스터 접근 준비는 남아 있다.

## 배경

웹이 시작한 이전(`POST /migrations`에 `target`)은 `cmp_api.start_request.start_input()`이 입력을 만든다. 이 함수는 `data_migration`을 넣지 않았다. 그래서 `MigrationWorkflow`는 구축 뒤 데이터 단계에서 `data-<id>` 자식을 띄우지 않고 scaffold `copy_data`로 넘어갔다. 그 결과 ticket 07의 두 번째 승인 API(`approve?gate=cutover`)는 신호를 보낼 자식이 없어 항상 404였고, 웹 화면에서는 CDC 복제와 DB 전환에 도달할 수 없었다. CLI(`scripts/start_migration.py --data-migration`)로 시작할 때만 실제 경로가 돌았다.

## 결정

| ID | 질문 | 결정 | 이유 |
| --- | --- | --- | --- |
| E1 | 입력을 어디서 받는가 | API 서버 환경 변수 하나(`WEB_START_DATA_MIGRATION`, `DataMigrationInput` JSON 객체). 웹 요청에는 필드를 더하지 않는다 | 원본 DB Pod, 앱 Secret 이름, 터널 주소는 운영자가 정하는 값이다. 리허설 설정(provisioning 티켓 57, `WEB_START_SANDBOX_SETTINGS`)과 같은 방식이고 CLI `--data-migration`과 같은 값이다 |
| E2 | 어디까지 검사하는가 | API가 `DataMigrationInput`과 그 안의 `app`(`SourceApp`), `settings`(`CutoverSettings`)까지 검사한다. 실패하면 실제 시작이 503 `DATA_MIGRATION_SETTINGS_INVALID`이고 값은 응답에 넣지 않는다 | 잘못된 값이 구축이 끝난 뒤에야 실패하면 대상 자원만 남는다. `cmp_data`는 같은 패키지라 API가 바로 검사할 수 있다 |
| E3 | 비어 있으면 | `data_migration`을 넣지 않는다(지금과 같은 scaffold) | 데모 서버에 값이 없을 때 기존 동작을 바꾸지 않는다 |

## 구현

- `cmp_api.start_request.data_migration_settings()`: 설정 문자열 → 검사한 `DataMigrationInput` JSON 또는 `None`
- `start_input(..., data_migration=)`: 값이 있으면 `data_migration` 키를 넣는다. 결과는 같은 값으로 만든 CLI `build_start_input`과 같다.
- `POST /migrations` 설명(OpenAPI)에 503 `DATA_MIGRATION_SETTINGS_INVALID` 추가
- `cmp_api.runtime_report`: `web_start_data_migration=on|off|error:<CODE>`
- 데모: `infra/demo/compose.yaml` api 환경 `WEB_START_DATA_MIGRATION: ${WEB_START_DATA_MIGRATION:-}`(worker에는 없음), `runtime.env.example` 빈 줄, `runtime-check.sh` 선택 항목, README 설정 표

## 시험

- `tests/test_web_start_request.py`: 값이 없으면 `data_migration` 없음, 값이 있으면 CLI 결과와 같음, 잘못된 값 5종(JSON 아님, 배열, 모르는 필드, `app` 누락 필드, `settings.image` 없음)은 503이고 값이 응답에 없음, scaffold 시작은 영향 없음
- `tests/test_demo_runtime.py`: api 전용 기본 꺼짐, 예시 줄 비어 있음, `runtime-check.sh` 포함, 점검 출력 on/off/error
- 전체 `uv run pytest`: 2819 passed, 31 skipped
- 로컬 e2e(2026-11-06): 로컬 API에 웹과 같은 `POST /migrations`(ON_PREM, `.env` 업로드)를 보내고 `WEB_START_DATA_MIGRATION`으로만 데이터 입력을 넣었다. 분석·리허설·계획·승인·구축은 결과만 돌려주는 대역 worker로 통과시켰고, 상위 Workflow·데이터 자식·데이터 Activity·SSE·승인 API는 실제로 돌렸다(k3d 실습, 초당 약 50건 쓰기). 준비 전 승인 409 → SSE `approval.required gate=cutover` → HTTP 승인 202 → 전환(쓰기 중단 5.96ms), 테이블 4개 행 수·체크섬 일치, 앱 쓰기 오류 0건, SSE 이벤트 64건 계약 검증 통과. 이 실행에서 전환 확인용 내부 테이블이 `cutover.verify`에 보여 ticket 09에서 뺐다.

## 남은 일

- 데모 서버 값: `WEB_START_DATA_MIGRATION`의 `source.peer_host`와 `target_peer_host`는 원본과 대상 사이 WireGuard 경로(ticket 06)가 정해져야 채울 수 있다. 결선 데모 기본 대상이 ON_PREM(`onprem-k3s-basic`)이면 `target_kubeconfig_ref`도 넣는다.
- worker 실행 환경: 데이터 Activity는 worker에서 kubectl로 원본·대상 DB Pod에 붙는다. 데모 worker 이미지에 kubectl, 원본 EKS 자격 증명(`aws eks update-kubeconfig`), 대상 접근(GKE면 gcloud, ON_PREM이면 `DATA_MIGRATION_KUBECONFIG_DIR`)이 있어야 한다.
- ~~웹 완료 판정~~ 정리(2026-11-07): 결선 데모에서는 `cutover.handover`를 발행하지 않는다. API `migration_status`는 Temporal 조회가 되면 `scaffold_complete`로 완료를 판정하고 `cutover.handover`(access done)는 조회가 안 될 때의 대체 경로에서만 본다. 웹은 cloud-migration-web#34부터 전환 계획 `handover`가 `[]`이면 인계 이벤트 없이 완료로 본다. 인계 항목(dns·origin·access)을 실제로 다루게 되면 Traffic 담당과 발행 주체를 다시 정한다.
- 트래픽 단계의 `traffic` 입력도 같은 이유로 웹 시작에서 빠져 있다(Traffic 담당 영역).
