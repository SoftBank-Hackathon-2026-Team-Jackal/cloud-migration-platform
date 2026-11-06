# Ticket 45 — 데모 서버 실행 설정 연결(분석·샌드박스·ENV·카탈로그)과 서버 점검 절차

## 상태
IN_PROGRESS (로컬 구현·검증 완료, 리뷰·서버 반영 승인 대기, 수용 기준 1은 협의 필요) — [Issue #180](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/180). 2026-11-06 조율 세션 지시로 착수(3차 병렬 묶음 47·48·53·55·45).

## 배경 (점검 A-03, D-08, D-09, 흐름 점검 N12, 재검사)
- A-03·D-08: 데모 `infra/demo/compose.yaml` worker에 분석 저장소·작업 폴더·Sandbox kubeconfig가 없다. 그래서 analysis 큐에는 문자열 인자를 받는 scaffold Activity만 등록되고, 실제 시작 입력(dict)을 보내면 `Failed decoding arguments / Expected value to be str, was dict`(부모 `CHILD_WORKFLOW_FAILED`)로 끝난다.
- N12: `.env` 저장소(`ENV_FILE_STORE_DIR`·`ENV_FILE_ENCRYPTION_KEY`)가 compose에 없다. 44 이후 실제 시작은 `env_file_ref`가 필수라 데모 서버에서는 `POST /env-files`·실제 시작이 503 `ENV_FILE_STORE_UNCONFIGURED`다(44 보고).
- 44: 대상 Profile 카탈로그 `PLANNING_TARGET_PROFILES_FILE`(`/opt/cmp/config/target-profiles.json`)은 연결됐지만 배치 절차·서버 점검이 없다.
- 재검사(조율 세션 전달): "`/env-files`가 `APPROVAL_ADMIN_KEY`를 요구하는데 웹은 이 키를 받지 않는다." → 코드 확인 결과 아래 R7.
- D-09: 데모 서버가 main보다 뒤에 있고, 서버 커밋·DB 리비전·워커 큐를 확인할 절차가 없다.

## 관련 요구사항
PROV-001, PROV-002, PROV-004, PROV-021. 정책: [제품 정책](../../docs/product/policies.md) `.env` 별도 업로드. 명세: [env-file-ref](../../specs/env-file-ref/spec.md), [analysis-verification plan](../../specs/analysis-verification/plan.md)·[runbook](../../specs/analysis-verification/runbook.md).

## 선행 작업
Blocked by: 21, 41, 43, 44, 46, ENV-01 (main 반영). 동시 진행: 47(ECR 프로필)·48(GCP 마운트)도 `compose.yaml`·`ensure-secrets.sh`·README를 조금 고친다. 이 티켓은 분석·Sandbox·ENV·카탈로그·`/env-files` 부분만 맡는다.

## 작업 범위
- 포함
  - compose worker의 분석·Sandbox·Claude 설정 이름·출처·기본값 연결(값은 `/opt/cmp/.env`, 기본은 지금과 같은 scaffold), 필요한 볼륨
  - `.env` 저장소: api·worker 공유 디렉터리, `ensure-secrets.sh`가 키를 없을 때만 생성
  - 선택 `.env` 줄 예시 `infra/demo/runtime.env.example`, 카탈로그 배치 절차
  - 읽기 전용 서버 점검 `infra/demo/runtime-check.sh`와 상태 보고 모듈(`python -m cmp_worker.runtime_report`, `python -m cmp_api.runtime_report`)
  - `/env-files` 운영자 키 확인과 회귀 테스트, README 서버 점검 체크리스트·반영 순서
- 제외
  - 박승철님 분석·Sandbox 코드 변경, 그 설정의 실제 값(kubeconfig·Digest 목록·LLM 키·비용 한도)
  - 웹 저장소 변경, `/opt/cmp/web.env` 값
  - 서버 변경(`.env`·파일 배치·재시작·Deploy demo), AWS·GCP 변경 — 아래 "승인 대기"

## 착수 결정 (2026-11-06)

| ID | 결정 | 근거 |
| --- | --- | --- |
| R1 분석 경로 스위치 | worker에 `ANALYSIS_ARTIFACT_STORE_DIR: ${ANALYSIS_ARTIFACT_STORE_DIR:-}`, `ANALYSIS_WORKSPACE_ROOT: ${ANALYSIS_WORKSPACE_ROOT:-}`. **기본은 빈 값(지금과 같은 scaffold)**. 실제 분석은 운영자가 `.env`에 두 줄(`/opt/cmp/artifact-store`, `/opt/cmp/analysis-workspaces`)을 넣을 때 켜진다. 작업 폴더는 호스트 `/opt/cmp/data/analysis-workspaces` | 실제 분석 Activity(`AnalysisActivities`)는 scaffold Activity와 **같은 이름에 dict 인자**다(`fetch_source` 등). 한 worker는 한 경로만 등록하므로 실제를 켜면 web 실제 시작(`target`)은 분석되지만 scaffold 시작(`target` 없음)이 분석에서 실패한다(`scan_network` 미등록·인자 디코딩). 어느 경로를 데모에 둘지는 D-08이 남긴 결정이라 조용히 고르지 않는다(협의 필요). 저장소는 `ARTIFACT_STORE_DIR`와 같아야 한다(runbook: 다르면 Planning이 요구사항을 못 읽음) |
| R2 Sandbox | `SANDBOX_KUBECONFIG: ${SANDBOX_KUBECONFIG:-}`, `SANDBOX_PREBUILT_IMAGES: ${SANDBOX_PREBUILT_IMAGES:-}`. kubeconfig 파일은 운영자가 `/opt/cmp/secrets/sandbox/kubeconfig`(root, 0600)에 두고 `/run/cmp/sandbox:ro`로 마운트한다. `ensure-secrets.sh`는 빈 디렉터리만 만든다. 작업 폴더는 코드 기본값(분석 작업 폴더 옆 `sandbox-fixes`) | 실제 루프는 전용 `k3d-cmp-sandbox` 컨텍스트만 받는다(코드가 다른 컨텍스트를 거절). 분석 D-08 결정은 "이 Mac의 로컬 k3d"이고 데모 EC2에는 Sandbox 클러스터가 없다(runbook 마지막 절). 경로만 정하고 값은 박승철님 협의. 경로가 있는데 파일이 없으면 worker가 시작에서 멈추므로(`SANDBOX_KUBECONFIG_NOT_FOUND`) 기본은 빈 값 |
| R3 Claude | `LLM_ENABLED: ${LLM_ENABLED:-false}`, `ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}`(서버 `.env`, 저장소 아님), 원장 `LLM_LEDGER_PATH=/opt/cmp/llm/ledger.json`, 즉시 중지 `LLM_KILL_SWITCH_PATH=/opt/cmp/llm/disabled`(호스트 `/opt/cmp/data/llm`). 모델·한도는 코드 기본값 | ADR-004: 켜기·키가 모두 있어야 비용이 생긴다. 원장은 재배포에도 남아야 한도가 지켜진다. 키 소유·한도는 협의 필요. api에는 주지 않는다 |
| R4 소스 토큰 | 연결하지 않는다(`SOURCE_TOKEN_RESOLVER` 기본 `none`, 공개 저장소만) | 웹 시작 `source.kind`는 `GITHUB_PUBLIC`뿐이다. `env` 해석기는 개발 전용이고 운영 해석기(GitHub 로그인)는 아직 없다 |
| R5 `.env` 저장소 | api·worker 모두 `ENV_FILE_STORE_DIR=/opt/cmp/env-files`(호스트 `/opt/cmp/data/env-files`, 두 서비스 모두 읽기·쓰기 마운트), `ENV_FILE_ENCRYPTION_KEY: ${ENV_FILE_ENCRYPTION_KEY:?…}`(필수). `ensure-secrets.sh`가 키가 없을 때만 base64url 32바이트(43자)로 만들고 이후 바꾸지 않는다 | worker도 같은 저장소를 읽는다: 분석 EnvPlan 이름(ENV-05), Sandbox·구축 `render_env`(ENV-06·ENV-04). worker는 업로드를 쓰지 않지만(업로드·삭제·`bind_migration`은 api·정리 스크립트) 저장소가 열 때 루트를 `chmod 0700`하므로 `:ro`로 두면 worker가 시작하지 못한다(로컬 실측 `EROFS`). 그래서 읽기·쓰기 마운트다. 키가 바뀌면 저장된 업로드를 모두 못 읽으므로 회전하지 않는다(백업은 `.env`와 함께). 필수(`:?`)로 둔 이유: `start.sh`가 compose 전에 `ensure-secrets.sh`를 실행하므로 항상 있다 |
| R6 카탈로그 | 44의 마운트(`/opt/cmp/config:ro`)와 예시(`target-profiles.example.json`)를 그대로 쓴다. 배치는 운영자가 `/opt/cmp/config/target-profiles.json`(root, 0644, 비밀 없음)에 실제 계정·프로젝트·State 버킷 값을 넣는다(승인 대기). 점검은 `runtime_report`의 `target_profiles=<이름들>` | 값은 환경마다 다르고 실제 계정·버킷이 들어가므로 저장소에 두지 않는다. 비밀은 없다(참조만) |
| R7 `/env-files` 인증 | **API 변경 없음.** `/env-files`는 이미 `X-Enrollment-Admin-Key`(= `AGENT_ENROLLMENT_ADMIN_KEY`)를 요구한다. `/connections`·`/agent-enrollments`와 같은 의존성(`routes.agents.require_admin`)이고 `APPROVAL_ADMIN_KEY`가 아니다(전달된 전제와 다름). 회귀 테스트로 고정. 실제 간극은 **데모 compose `web`이 이 키를 받지 않는 것**(21 결정: 웹 `/api/agents`에 로그인 확인이 없어서)이다. 웹 실제 모드는 운영자가 `/opt/cmp/web.env`에 `PLATFORM_ENROLLMENT_ADMIN_KEY`를 둬야 연결·`.env` 업로드·실제 시작이 된다. compose가 키를 웹에 자동으로 넣지는 않는다 | 웹 저장소 main은 이미 `PLATFORM_ENROLLMENT_ADMIN_KEY`를 `X-Enrollment-Admin-Key`로 보낸다(연결 관리). 웹에 운영자 키를 주는 것은 공개 웹의 접근 통제 결정이라 21처럼 운영자 승인 단계로 남긴다(협의 필요: 웹이 이 경로들을 로그인으로 막는지) |
| R8 빌드 값 | 코드·compose 변경 없음. 절차는 28 그대로(`deploy-build-stack.sh` 출력 다섯 줄 → `.env` → 릴리스). 점검은 worker `build=on/off/error:CODE`, api `platform_ecr_registry=on/off` | 47·48과 겹치지 않게 절차·점검만 정리 |
| R9 서버 점검 | `infra/demo/runtime-check.sh [/opt/cmp]`: 릴리스 커밋, 컨테이너 상태, `alembic current`, worker·api 상태 보고, IMDS 가드 규칙·서비스(54), `.env` **이름만**, 필수 누락·선택 미설정 이름, 카탈로그·kubeconfig·GCP 키 파일 존재 여부. 상태 보고는 worker가 시작 때 쓰는 함수(`build_pipeline`·`build_sandbox_loop`·`BuildSettings.from_values`·`build_llm_factory`·`optional_env_file_store`)를 그대로 불러 `real/scaffold/on/off/error:CODE`만 출력한다 | 서버에서 값 출력 없이 "무엇이 켜졌나"를 한 번에 본다. 같은 함수를 부르므로 점검과 실제 등록이 어긋나지 않는다 |

## 수용 기준 (Issue #180)
- [ ] 데모 서버에서 실제 입력으로 시작한 이전이 분석 → 샌드박스 → Planning 인계까지 간다. — **BLOCKED(협의 필요)**: 데모 서버에 Sandbox 클러스터(`k3d-cmp-sandbox`)가 없고(분석 D-08 결정), R1의 경로 선택이 필요하다. 로컬 compose 실측은 아래 "검증".
- [x] 서버의 커밋·alembic 리비전·워커 큐를 확인하는 절차가 있다. — `runtime-check.sh`, README "서버 점검 (티켓 45)".
- [x] (N12) `.env` 저장소 설정이 api·worker에 있고 키는 없을 때만 생성된다.
- [x] (44) 카탈로그 배치 절차와 점검 명령이 있다.

## Test Seam
- 정적 계약: `tests/test_demo_runtime.py`가 compose 환경·볼륨, `runtime.env.example`의 값과 마운트 대상, `start.sh` 디렉터리 생성을 단언한다.
- `ensure-secrets.sh` 실행: 키가 `load_key`로 32바이트이고 두 번 실행해도 같다, `secrets/sandbox` 0700 빈 디렉터리.
- 상태 보고: `cmp_worker.runtime_report.report(Settings(...))`가 빈 설정이면 scaffold·off, 설정이면 `real`, 잘못된 설정이면 `error:<CODE>`. `cmp_api.runtime_report.report()`가 저장소·카탈로그 상태를 코드로.
- 점검 스크립트: `docker`·`iptables`·`systemctl`을 가짜로 둔 실행에서 `.env` 값이 출력에 없고 이름·누락 목록이 나온다.
- `/env-files` 키: `tests/test_env_files.py::test_operator_key_is_the_connection_and_agent_key_not_the_approval_key`(승인 키만으로는 403, 연결과 같은 의존성). 기존 동작을 고정하는 테스트라 처음부터 통과했다.

## 검증
- RED: 새 테스트 8개 실패(설정·파일·모듈 없음), `/env-files` 키 테스트는 통과(기존 동작 고정).
- GREEN: `uv run pytest tests/test_demo_runtime.py tests/test_env_files.py` 51 passed(로컬 실측에서 나온 두 수정 포함).
- `make check`: 통과(ruff, 전체 pytest).
- 로컬 compose 통합 실행: 아래 "로컬 실측".

## 로컬 실측 (2026-11-06, `d-repro` 방식)
데모 `compose.yaml`을 그대로 쓰고 호스트 경로·포트·이미지 이름만 바꾼 override(프로젝트 `cmp-t45`, arm64 로컬 빌드)로 api·worker·Temporal·DB를 띄웠다. 키는 이 브랜치의 `ensure-secrets.sh`로 만들고 카탈로그는 예시를 그대로 뒀다. 클라우드 자격 증명·Docker 소켓은 주지 않았다. 웹과 같게 `POST /env-files`(운영자 키) → `POST /migrations`(`target` AWS `aws-eks-basic`, `source` GITHUB_PUBLIC `octocat/Hello-World`, `env_file_ref` 문자열)로 시작했다. 끝난 뒤 컨테이너·볼륨·이미지·임시 파일을 지웠다.

| 실행 | 설정 | 결과 |
| --- | --- | --- |
| 0 | 처음 compose(worker `env-files:ro`) | **worker 재시작 반복**: `EncryptedEnvFileStore.__init__`의 `chmod` → `OSError: [Errno 30] Read-only file system`. R5를 읽기·쓰기 마운트로 고침 |
| A | 기본(`.env`에 분석 줄 없음) | 업로드 201(키 이름만), 시작 202, `alembic` `20261106_02`, worker 보고 `analysis=scaffold sandbox=scaffold build=off llm=off env_store=on`, api 보고 `env_store=on target_profiles=aws-eks-basic,gcp-vm-basic,onprem-k3s-basic`. 약 4초 뒤 `failed`/`CHILD_WORKFLOW_FAILED`/`analysis`(D-08 재현, 의도한 기본) |
| B1 | `runtime.env.example`의 `ANALYSIS_*` 두 줄 | 보고 `analysis=real`. `fetch_source`가 `ANALYSIS_INPUT_INVALID`(비재시도). 직접 호출하니 원인은 **worker 이미지에 `git` 없음**(`FileNotFoundError: 'git'`). Dockerfile worker 단계에 `git`을 남기고 `start.sh` 도구 검사에 `git --version` 추가 |
| B2 | B1 + `git` 든 이미지 | `fetch_source`·`scan_code`·`scan_manifests`·`scan_aws_infra` 통과 → `build_requirements`가 `ANALYSIS_BLOCKED`(Requirements could not be built). 앱·매니페스트가 없는 공개 저장소라 예상된 정지 |
| B3 | B1 설정에서 scaffold 시작(`target` 없음) | 분석에서 `CHILD_WORKFLOW_FAILED`(R1 트레이드오프 확인) |

어디까지 갔나: **실제 분석 스캔 단계까지**. 그 뒤는 실측하지 못했다. 이유: 데모 앱 `demo-app`은 비공개이고 웹 시작은 `GITHUB_PUBLIC`·해석기 `none`이라 가져올 수 없으며, 쓸 수 있는 공개 Go 앱 표본이 없다. 그다음 정지 지점은 코드로 본 추정이다: 분석 검토 게이트(사람 확정 Signal, 시간 제한 없음) → `assemble_sandbox_input`(analysis 큐, 실제 경로에 등록됨) → `SandboxVerificationWorkflow` dict 경로의 `sbx_build`가 Sandbox 루프 없이 미등록이라 진행하지 못함(데모 서버에 `k3d-cmp-sandbox` 없음).

## 미검증
- 데모 서버 실제 상태: 서버 변경과 Deploy demo는 승인 대기. SSM은 `.env` 이름 읽기만 시도(결과는 PR 본문).
- amd64 이미지: 로컬은 arm64(Mac)로 빌드했다.
- 실제 Sandbox 루프·Claude 호출: 데모 서버에 클러스터·키가 없다(협의 필요).
- 분석 검토 게이트 이후(실제 앱 표본 없음, 위 "로컬 실측").
- worker의 Helm은 v4.3.1(21·15 고정)인데 분석 사전 점검(`cmp_worker.preflight`)은 Helm 3을 요구한다. 서버에서 실제 Sandbox 루프를 돌리면 확인 필요(협의 필요 (6)).

## 협의 필요
- 박승철님: (1) 데모에서 어느 분석 경로를 둘지(R1: scaffold 유지 또는 실제). 실제를 켜면 scaffold 시작이 분석에서 실패하므로, 두 경로를 같이 두려면 scaffold Activity가 dict도 받거나 실제 경로를 다른 이름으로 등록해야 한다(분석 코드). (2) 데모 서버용 Sandbox 클러스터를 둘지, 두면 kubeconfig 전달 방법(R2). 없으면 실제 시작은 `sbx_build`에서 멈춘다. (3) `SANDBOX_PREBUILT_IMAGES` 값 또는 `BUILD_*` 켜기 시점. (4) Claude 키 소유·한도(R3). (5) Sandbox가 없는 worker에서 실제 입력이 오면 시작 때 명확한 코드로 거절할지(D-08 수정 방향). (6) 서버 worker의 Helm 4로 Sandbox Chart 배포가 되는지(사전 점검은 Helm 3 요구).
- 웹(김민서·웹 담당): 실제 모드에서 `/opt/cmp/web.env`에 `PLATFORM_ENROLLMENT_ADMIN_KEY`를 둘지와 그 전에 웹의 연결·`.env` 업로드·Agent 경로를 로그인으로 막는지(R7). 웹 main에는 아직 `/env-files` 호출이 없다(연결 관리만 실제 API).

## 결선 데모 기본 대상 (2026-11-06, P2)
- 결정: 결선 데모 카탈로그는 `infra/demo/target-profiles.demo.json`(`onprem-k3s-basic` 하나, 예시 항목과 같은 값)이다. 웹 기본 대상이 이미 `onprem`(`FlowApp`)이고, 데모 서버에서 실제로 구축할 수 있는 대상은 Agent가 연결된 K3s뿐이다(AWS_TARGET 연결·GCP 키 없음). 코드 기본값은 두지 않는다(44 S6 그대로). GCP·AWS 시작은 분석 뒤가 아니라 시작에서 422 `TARGET_PROFILE_NOT_FOUND`다. 클라우드 대상이 준비되면 예시 카탈로그로 바꾼다(재시작 불필요).
- Agent ID·터널 주소(`10.200.0.2`)는 카탈로그 값이 아니라 Agent 등록·터널 배정 값이다(16 T4). 웹은 `GET /agent-admin/agents`의 `agent_id`를 보낸다.
- 점검: `tests/test_web_start_request.py`의 결선 테스트가 데모 카탈로그로 웹 시작 → Plan 게시 → 웹 계획 조회 → APPROVE(웹 본문) → claim → Agent 작업(등록 Agent·`cluster_uid`·카탈로그 Namespace·StorageClass)까지 잇는다. 승인 Workflow → `execute_on_prem_plan` → Agent 보고는 `tests/test_onprem_rollout.py`가 덮는다.
- 서버 반영 뒤 `runtime-check.sh`의 api 줄 기대값은 `target_profiles=onprem-k3s-basic`이다.

## 문서 변경
- `infra/demo/README.md`: "분석·Sandbox·ENV 실행 설정 (티켓 45)" 절(설정 표, 반영 순서, 카탈로그 배치, 서버 점검 체크리스트), 설정 표에 ENV 행. 결선: 반영 2단계 카탈로그를 `target-profiles.demo.json`으로.
- `specs/provisioning/tasks.md`: 45 행.
- `specs/analysis-verification/runbook.md`: 배포된 플랫폼 절에 데모 서버 설정 이름과 경로 스위치 링크.
- `docs/engineering/env-file-guide.md`: 데모 서버 저장소 위치·키 생성 한 줄.
- `infra/demo/Dockerfile`·`start.sh`: worker 이미지에 `git`(실제 분석 소스 가져오기), 릴리스 도구 검사에 `git --version`.

## 완료 체크리스트
- [x] 착수 결정 기록
- [x] RED → GREEN
- [x] `make check`
- [x] 로컬 compose 실측 기록
- [ ] 리뷰
- [ ] 서버 반영(승인 대기)
