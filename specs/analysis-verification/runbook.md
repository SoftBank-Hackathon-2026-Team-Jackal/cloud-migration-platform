# 실행 안내: 분석·Sandbox 검증 첫 사이클을 로컬에서 돌리기

이 문서는 로컬에서 실제 경로(분석 → 검토 → Sandbox 루프 → 발행)를 돌리는 순서다. 데모와 개발용이며, 운영 배치는 다루지 않는다. 먼저 `uv run python scripts/preflight.py`로 빠진 것을 확인한다(비용 없음, 아무것도 바꾸지 않음).

## 1. 준비물 (한 번만)

| 항목 | 내용 |
| --- | --- |
| 도구 | `git`, `helm` 3 계열(4는 안 됨), `kubectl`, Docker 런타임(Colima 등), `uv` |
| Sandbox 클러스터 | 전용 `k3d-cmp-sandbox`. 만드는 법은 [티켓 11의 환경 준비](../../tickets/analysis-verification/11-sandbox-apply.md). 다른 컨텍스트는 코드가 거절한다 |
| Temporal·DB | `make infra-up`(Docker Compose). 주소는 `TEMPORAL_ADDRESS`(기본 `localhost:7233`) |
| 모델 키 | Claude를 쓰려면 워커를 띄우는 셸에 `ANTHROPIC_API_KEY`를 내보낸다. `.env` 파일이나 저장소에는 적지 않는다 |
| 분석할 앱 | 공개 URL이면 그대로. 비공개 GitHub 저장소는 개인 토큰을 워커에만 주고(`SOURCE_TOKEN_RESOLVER=env`, 환경 변수 `CMP_GITHUB_TOKEN`, 읽기 권한이면 충분) 시작할 때 `--source-access dev-env`를 준다. 로컬 복제본을 쓰려면 `ANALYSIS_ALLOW_LOCAL_REPOSITORIES=true`(개발 전용)를 켠다. 토큰은 `.env`에 적지 않고 워커를 띄우는 셸에서만 내보낸다 |
| 앱 이미지 | 기본은 이미 빌드·푸시된 이미지(Digest 고정). `BUILD_*` 설정을 모두 주면 실제 빌드 경로를 쓴다(아래 "실제 빌드 켜기", D-06·D-12·승인 후). 레지스트리가 비공개면 k3d가 받을 수 있게 공개하거나 pull 자격 증명을 넣어야 한다 |

## 2. 설정 (`.env`, 값은 예시)

`.env.example`의 항목을 `.env`로 복사해 채운다. 경로는 **절대 경로**로 쓴다(`~`와 `$HOME`은 펼쳐지지 않을 수 있다). 분석 결과 저장소는 **분석 워커와 Sandbox 워커, Planning이 같은 디렉터리**를 가리키게 한다. Planning은 요구사항을 `<저장소>/<SHA-256>`로 읽는다.

```bash
ANALYSIS_ARTIFACT_STORE_DIR=/절대/경로/cmp-data/store
ARTIFACT_STORE_DIR=/절대/경로/cmp-data/store          # Planning이 읽는 곳(같은 값)
ANALYSIS_WORKSPACE_ROOT=/절대/경로/cmp-data/workspaces
SANDBOX_KUBECONFIG=/절대/경로/.kube/cmp-sandbox.yaml
SANDBOX_PREBUILT_IMAGES={"orders-api": "ghcr.io/<소유자>/<앱>@sha256:<digest>"}
LLM_ENABLED=true
LLM_LEDGER_PATH=/절대/경로/cmp-data/llm-ledger.json
```

모델 호출 한도(마이그레이션당 $3, 하루 $20, 한 달 $100)는 `LLM_LIMIT_*`로 바꾼다. 모델을 즉시 멈추려면 `LLM_KILL_SWITCH_PATH`로 지정한 파일을 만들거나 `CMP_LLM_DISABLED=1`을 건다.

## 3. 기동

```bash
make infra-up
uv run python scripts/preflight.py        # FAIL이 없어야 한다
ANTHROPIC_API_KEY=... make workers        # 모든 큐의 워커
```

`preflight`가 `FAIL`을 내면 그 줄의 설명대로 고친다. `WARN`은 돌 수는 있지만 일부가 빠진다는 뜻이다(예: 모델이 꺼져 있으면 매핑과 수정이 실행되지 않는다).

## 4. 실행

```bash
uv run python scripts/start_migration.py --project-id demo-1 \
  --repository /path/to/demo-app --commit <커밋 SHA> --manifests-dir k8s \
  --sandbox '{"write_path": "/api/messages", "read_path": "/api/messages/{{id}}"}'
```

Workflow ID가 출력된다. `--sandbox`는 선택이며 표준 DB 쓰기·읽기 테스트의 경로를 알려 준다. 없으면 헬스 테스트만 돈다.

## 5. 검토에 답하기

분석이 매핑까지 끝나면 `analysis-<Workflow ID>` 자식이 필수 기능 확정을 기다린다.

```bash
uv run python scripts/review_signal.py state   <Workflow ID>
uv run python scripts/review_signal.py confirm <Workflow ID> --version <버전> --actor me --required <행ID>=true ...
uv run python scripts/review_signal.py approve <Workflow ID> --version <버전> --actor me --approve <행ID>=APPROVED ...
```

`BLOCK`이 있으면 Sandbox로 가지 않고 사유가 보고된다. 통과하면 Sandbox 루프가 돌고, 실패하면 분류·수정·갱신을 최대 3회 반복한다. 루프 중 처리가 바뀐 행이 있으면 `sandbox-<Workflow ID>` 자식에 `approve_changed_rows` Signal로 다시 승인한다.

보안 항목(티켓 29-B): `state`의 `security`에 원본·대상 상태와 판정이 나온다. 판정이 `LOWER`인 항목(`approvable: true`)은 같은 `approve` 신호에 `security:<기준>`으로 답한다(`--approve security:EXTERNAL_HTTPS=APPROVED`). 필수 기능 확정(`confirm`) 뒤에 열리고, 모든 승인 대상(매핑 행과 보안 항목)에 한 번에 답해야 한다. 거절(`REJECTED`)하면 이전이 막히고 사유와 제안 코드가 `blocked`에 남는다. `UNKNOWN`·약점·`SAME`·`HIGHER`는 승인 대상이 아니다. `proof`가 `POST_DEPLOY`인 항목(외부 HTTPS, 입구의 경로 차단)은 Sandbox로 증명되지 않고 배포 후 측정이 필요하다.

## 6. 결과 확인과 정리

- 부모 Query `status`의 `pipeline`에 분석·요구사항 참조와 발행 결과가 있다. 종료 상태는 `scaffold_complete`(통과), `failed`(원인은 `failure_code`), `cancelled`다.
- 발행된 최종 분석 버전·요구사항·패치 산출물은 저장소 디렉터리에 해시 이름 파일로 있다.
- Sandbox 네임스페이스(`cmp-sbx-*`)는 통과·실패·취소 모두 워커가 지운다. 남았다면 `kubectl --kubeconfig $SANDBOX_KUBECONFIG --context k3d-cmp-sandbox get ns`로 찾아 지운다(라벨 `cmp.sandbox/expires-at`이 만료 시각이다). 루프 끝의 삭제는 요청만 보내고 기다리지 않는다(티켓 32). 끝나지 않은 것은 15분마다 도는 Temporal Schedule `cmp-sandbox-expire-sweep`이 만료 뒤 지운다(sandbox 큐 워커가 있어야 실행됨).

### Sandbox 상태 보기와 정리

```bash
uv run python scripts/sandbox_ctl.py status
uv run python scripts/sandbox_ctl.py cleanup                          # 만료된 것만
uv run python scripts/sandbox_ctl.py cleanup --all --confirm DELETE-ALL  # 전부(확인어 필요)
uv run python scripts/sandbox_ctl.py prewarm                          # postgres·프록시 이미지를 노드에 미리 받기
```

전용 클러스터(`k3d-cmp-sandbox`)의 `cmp-sbx-*`만 다룬다. `prewarm`은 클러스터를 새로 만든 뒤 한 번 실행한다(반복해도 안전, 이미 받은 이미지는 다시 받지 않는다). 만료된 Sandbox는 워커의 Schedule이 지우고, 그 워커가 없을 때는 `cleanup`을 직접(또는 cron·launchd로 15분마다) 실행한다. 출력에는 네임스페이스·실행 ID·만료 시각만 나온다.

### 표준 테스트의 쓰기 모양 (티켓 27)

DB 쓰기·읽기와 재시작 후 유지 테스트는 앱이 쓰기를 받는 모양을 알아야 한다. 모양을 주지 않으면 헬스 테스트만 돈다. 모양은 `--sandbox`에 설정으로 준다(Go 데모 앱 예시).

```bash
--sandbox '{"write_path": "/api/messages", "write_body": {"client_msg_id": "std-{{run_id}}", "author": "cmp-test", "body": "value-{{run_id}}"}, "write_status": 201, "id_from_response": "id", "value_field": "body", "read_back": "REPEAT_WRITE"}'
```

- `REPEAT_WRITE`는 쓰기가 멱등인 앱(같은 키 재전송에 200과 저장된 행)을 위한 읽기 방식이다. 같은 쓰기를 다시 보내 같은 ID와 같은 본문이 오는지로 저장을, DB 재시작 뒤에 오는지로 유지를 확인한다. 케이스마다 쓰기 키가 달라서 첫 쓰기는 항상 새 행이다.
- "DB 재시작 후 유지" 테스트는 재시작 직후 앱이 5xx를 주거나 답이 없는 동안(최대 60초) 같은 요청을 다시 보낸다(데모 앱은 재시작 직후 "같은 ID로 재시도하라"는 503을 준다). 데이터를 잃어 바로 201이 오면 기다리지 않고 실패한다.
- 테스트 실패 사유 코드(`STATUS_MISMATCH expected=200 actual=503` 같은 상태·필드 값)는 Sandbox 결과의 테스트별 `reason`에서 본다.
- 이 모양은 앱마다 사람이 적어야 해서 일반적이지 않다. 후보를 자동으로 고르는 방식은 만들었다가 후보가 앱 하나뿐이라 제거했고, 일반화는 소스 근거를 쓰는 AI 테스트로 간다.

### 보안 프로브 (티켓 30)

`--sandbox '{"security_probes": true}'`(웹 시작이면 서버의 Sandbox 기본 설정)로 켜면 표준 묶음에 DB 접근 프로브가 더해진다. 요구사항의 통신 의도에서 만들어지고(앱 전용 값 없음), Sandbox가 시험 Pod로 접속을 시도해 판정한다: 의도에 적힌 출발지는 DB에 연결되고, 그 밖의 Pod는 차단되어야 통과한다. 케이스 ID는 `sec-db-allowed-<출발 리소스>-<DB>`, `sec-db-outsider-<DB>`이고 결과에 `PROBE_MISMATCH expected=… actual=…`(기대와 다름)나 `PROBE_UNAVAILABLE`(시험을 못 돌림, 통과가 아님)이 사유로 실린다. 시험 Pod(`cmp-probe-…`)는 끝나면 지워지고 남아 있으면 Sandbox 정리 때 Namespace와 함께 사라진다. 이 클러스터는 NetworkPolicy를 적용하는 CNI여야 의미가 있다(k3d 기본은 적용). 평문 DB 거절과 운영용 경로 차단 프로브는 아직 없다(대상 DB에 SSL이 없고 운영 경로는 사용자 확정이 없음).

### AI 생성 테스트

`--sandbox`에 `"ai_tests": true`를 주면 첫 Sandbox가 정상 기동한 직후 한 번 Tester를 부른다(실제 Claude 호출이므로 비용 승인이 먼저다. 모델이 설정되지 않았으면 `LLM_NOT_CONFIGURED`로 건너뛴다).

- Tester는 소스의 라우트 목록과 읽기 전용 소스 조각 도구, `test-authoring` 스킬로 앱을 읽고 **쓰기 모양(`shapes`)과 읽기 요청(`probes`)** 을 소스 근거와 함께 낸다. 기대값은 쓰지 않는다. 원본 쪽 테스트 자원(D-09)이 없어도 호출된다.
- 쓰기 모양은 왕복(`ai-<id>-roundtrip`)과 재시작 후 유지(`ai-<id>-restart`) 테스트로 만든다. 읽기 요청은 앱에 두 번 보내 같은 응답이면 `ai-0rec-<id>` 테스트가 되고, 응답이 매번 달라지면(`RECORD_UNSTABLE <필드>`) 비2xx·JSON 아님·비어 있음·너무 큼·민감해 보이는 값이면 버린다.
- 결과 `ai_tests`의 `accepted`/`rejected`에서 어떤 테스트가 왜 빠졌는지 본다: `NO_EVIDENCE`(근거 없음), `EVIDENCE_FILE_MISSING`, `EVIDENCE_LINE_MISSING`, `EVIDENCE_MISMATCH <경로>`(인용한 줄 근처에 그 경로가 없음), `PASSES_WITHOUT_DEPENDENCY`(의존성을 끊어도 통과), `RESET_FAILED`(기록 뒤 데이터를 기준 상태로 못 되돌림).
- 이 방식이 보장하는 것은 "기준 시점 이후 Fixer의 코드 수정이 동작을 바꾸지 않았다"까지다. 기준 상태가 AWS 원본과 같다는 것은 보장하지 않는다. 정답이 앱 규칙에서 나오는 값(계산 결과 등)은 원본 자원(D-09)이 없으면 테스트가 되지 않고 공백으로 남는다.
- `get_snippet`이 읽을 수 있는 것은 가져온 소스뿐이며 `.env`와 키·인증서 파일은 읽지 않는다.

## 7. 자주 막히는 곳

| 증상 | 원인과 조치 |
| --- | --- |
| 단계가 `sbx_build`에서 실패, 코드 `BUILD_NOT_CONFIGURED` | `SANDBOX_PREBUILT_IMAGES`가 비어 있다. 앱마다 Digest 고정 이미지를 적는다 |
| `PREBUILT_IMAGE_NOT_DIGEST_PINNED` | 태그(`:latest`)는 거절한다. `@sha256:` 형태로 적는다 |
| 커밋·저장소 값 거절(`not a plain value`) | `-`로 시작하거나 공백·제어 문자가 있다. 값을 고친다 |
| `LLM_DISABLED`, `BUDGET_RUN/DAY/MONTH` | 모델이 꺼졌거나 한도에 닿았다. 정상 동작이며 사람 검토로 넘어간다 |
| Planning이 요구사항을 못 읽음 | `ARTIFACT_STORE_DIR`와 `ANALYSIS_ARTIFACT_STORE_DIR`가 다르다 |
| 앱이 시작하지 않음 | 첫 시도는 포트·헬스 경로가 틀릴 수 있고, 이 경우 SPEC으로 분류되어 Fixer가 고친다 |

## 8. Sandbox 앱에 들어가는 값

- DB 연결 변수(`DB_HOST`·`DB_PORT`·`DB_NAME`·`DB_USER`·`DB_PASSWORD`·`DATABASE_URL` 등 이름 패턴)는 분석이 대상 DB 출력에 묶고, Sandbox가 새로 띄운 DB 값으로 채운다. 비밀번호는 실행마다 새로 만든 Secret 참조이고 원본 값은 복사하지 않는다.
- `AWS_*`는 복사하지 않는다. 나머지 변수는 사용자가 올린 `.env`의 참조(`env_file_ref`)로 처리한다(ENV-05·06).
  - 시작할 때 `--env-file-ref '{"id": "...", "version": "1", "sha256": "..."}'`(`POST /env-files`가 돌려준 값)를 주면 분석은 키 이름만 읽어 키마다 처리 규칙(`EnvPlan`: `PASS`·`EXCLUDED`·`REPLACED`·`UNREPRESENTABLE`·`MISSING`)을 정하고, Sandbox는 같은 `render_env`로 앱 Secret(`<앱>-env`)과 DB 연결 Secret(`<DB>-conn`)을 만든다. 값은 `PASS` 키만 읽고 그것도 클러스터에 적용하는 순간뿐이다.
  - `UNREPRESENTABLE`(예: `DB_DSN`)이나 코드는 읽는데 선언되지 않은 `MISSING` 키가 있으면 Sandbox를 만들기 전에 `NEEDS_REVIEW`로 멈춘다(이유 `SANDBOX:ENV_UNREPRESENTABLE`, `SANDBOX:ENV_MISSING`).
  - 분석과 Sandbox 워커에 플랫폼의 `.env` 저장소 설정(`ENV_FILE_STORE_DIR`, `ENV_FILE_ENCRYPTION_KEY`)이 있어야 한다. 없으면 `ENV_FILE_STORE_UNCONFIGURED`, 보관 기간이 지났으면 `ENV_FILE_EXPIRED`로 재시도 없이 실패한다.
  - `env_file_ref`를 주지 않으면 예전 방식 그대로다: 분석은 저장소의 `.env*` 파일에서 이름을 읽고, Sandbox는 `--sandbox`의 `env_names`와 `env_file`(워커 로컬 파일)로 값을 받는다.
- 코드에 직접 적힌 주소는 바꿀 수 없다. 이 경우 실패로 분류되어 Fixer가 코드를 고친다(파일 20개·파일당 200KB 이하, 테스트·`.env`·`.git`은 수정 불가, 최대 3회).

### 실제 빌드 켜기 (티켓 25)

`.env`에 아래 네 항목이 모두 있으면 앱 이미지를 사람이 적은 Digest 대신 실제 경로로 만든다: 소스 묶음을 `BUILD_SOURCE_BUCKET`에 올리고, 민서님의 Build 모듈로 `BUILD_ECR_REPOSITORY`에 빌드하고, 결과를 `BuildAttempt` 증거로 남긴다. 하나도 없으면 `SANDBOX_PREBUILT_IMAGES`를 쓰고(빌드 증거 없음), 일부만 있으면 워커가 시작하지 못한다(`BUILD_SETTINGS_INCOMPLETE`).

```bash
BUILD_SOURCE_BUCKET=<소스·테스트 묶음 버킷>
BUILD_ECR_REPOSITORY=<계정>.dkr.ecr.<리전>.amazonaws.com/<저장소>
BUILD_LOG_BUCKET=<빌드 로그 버킷>
BUILD_PLATFORM=linux/amd64
BUILD_AWS_PROFILE=<이름 있는 프로필, 승인된 것만>   # 선택
```

- 실제 AWS 호출이므로 버킷·ECR·프로필 사용 승인과 D-06·D-12 결정이 먼저다. 사전 점검(`scripts/preflight.py`)은 설정 유무만 보고 계정을 호출하지 않는다.
- 빌드가 이미지를 못 만들면 Sandbox 없이 `NEEDS_REVIEW`(이유 `BUILD:<코드>`)로 사람에게 넘어간다. 빌드 모듈은 보안상 원본 오류를 숨기므로 `EXTERNAL_COMMAND_FAILED`만 보이면 아래 항목을 먼저 본다.
- 이 컴퓨터(Mac, Colima)에서 처음 돌릴 때 확인한 것(2026-11-06, 개인 계정으로 S3 업로드 → 빌드 → ECR push → Digest 조회 → 재사용까지 성공):
  - 필요한 도구는 `aws` CLI와 `docker buildx`다. Homebrew로 설치한 buildx는 `~/.docker/cli-plugins`에 링크해야 Docker가 찾는다.
  - 빌드 모듈은 로그인 정보를 격리하려고 `DOCKER_CONFIG`를 임시 폴더로 바꿔 쓴다. 그러면 Docker가 **플러그인(buildx)과 현재 컨텍스트(Colima)를 둘 다 못 찾는다**. 서버는 시스템 경로의 buildx와 기본 소켓이 있어 문제가 없다. 이 컴퓨터에서는 워커 환경에 `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock`을 주고, buildx를 시스템 플러그인 폴더(`/usr/local/lib/docker/cli-plugins`)에 두거나 임시 `DOCKER_CONFIG`에 링크해 주는 `docker` 래퍼를 PATH 앞에 둔다.
  - 빌드 모듈은 `AWS_PROFILE`(환경변수)로 `aws`를 부른다. `BUILD_AWS_PROFILE`은 소스 업로드용이므로 같은 프로필을 `AWS_PROFILE`로도 내보낸다.
  - ECR은 **레이어가 0개인 이미지를 거절**한다(`FROM scratch`만 있는 Dockerfile). 시험용 소스에도 파일 하나를 `COPY`해야 한다.
  - ECR 저장소는 태그 변경 불가라 같은 태그를 다시 올릴 수 없다. 시도 ID(`<run>-b<n>`)가 태그에 들어가므로 시험은 실행 ID를 새로 만든다.

### 비공개 ECR 이미지 pull (티켓 26)

실제 빌드가 올린 이미지는 플랫폼 ECR에 있어 비공개다. `BUILD_ECR_REPOSITORY`가 있으면 Sandbox를 적용할 때마다(갱신 포함) `BUILD_AWS_PROFILE`의 신원으로 ECR 토큰을 새로 받아 네임스페이스에 `cmp-ecr-pull` Secret을 만들고 `default` ServiceAccount가 쓰게 한다. 토큰은 표준 입력으로만 전달하고 어디에도 남기지 않는다.

- 공개 이미지(`PrebuiltImages`)만 쓰면 아무것도 만들지 않는다.
- 플랫폼 레지스트리와 저장소 밖의 ECR 이미지는 `IMAGE_REGISTRY_NOT_ALLOWED`, 토큰 실패는 `ECR_TOKEN_UNAVAILABLE`·`ECR_ACCOUNT_MISMATCH`로 멈춘다.
- 실제 토큰 발급은 AWS 호출이므로 프로필 사용 승인이 필요하다.

### Planning 인계 (티켓 15)

시작할 때 `--planning`에 대상 선택과 대상 설정을 JSON으로 주면(`.env` 참조는 시작 요청의 `--env-file-ref`를 쓰며, `--planning` 안에 따로 줘도 된다), Sandbox가 통과한 직후 Planning 입력을 조립해 등록한 뒤 계획 단계로 간다.

```bash
uv run python scripts/start_migration.py ... --planning '{"target_selection": {...}, "target": {...}, "env_file_ref": {"id": "...", "version": "1", "sha256": "..."}}'
```

- 등록은 민서님의 Planning 입력 등록 함수를 워커가 호출하는 방식이다(관리자 토큰 불필요). 분석 저장소와 Planning 저장소(`ARTIFACT_STORE_DIR`)는 같은 디렉터리여야 한다.
- 거절은 부모 `failed`와 `failure_code`로 나온다: `BUILD_EVIDENCE_MISSING`(빌드 증거 없음 — 미리 빌드한 이미지 경로), `TEST_SUITE_UPLOAD_NOT_CONFIGURED`, `SANDBOX_NOT_PASSED`, `PROJECTION_REJECTED`, `PLANNING_INPUT_INVALID`, `TARGET_SELECTION_CHANGED`, 등록 쪽 코드(`MIGRATION_MISMATCH` 등).
- 분석이 만든 `EnvPlan`(키 이름과 처리 결과만)을 등록에 함께 넣는다. 참조가 없으면 `ENV_FILE_REF_MISSING`, 계획이 다른 `.env`의 것이면 `ENV_PLAN_REF_MISMATCH`로 멈춘다.

### 같은 테스트 묶음을 Production에서 다시 돌리기

Planning에 넘긴 `test_suite_ref`는 S3의 `tests/<sha256>.tar.gz`(아카이브 해시)다. 아카이브 안의 `suite.json`이 `run_suite`가 읽는 묶음이고, 그 파일의 해시가 고정 해시다. 대상 주소와 인증 참조만 바꿔 같은 함수로 돌린다.

```python
# suite.json을 꺼내 저장소에 두고, 그 파일의 SHA-256로 참조를 만든다
result = run_suite(
    bundle_ref, "https://<Production 앱 주소>", auth_ref=None, include="ALL", store=store
)
```

순서: 배포 직후에는 준비 검사(Pod 준비, DB 연결, 헬스)만 하고, 데이터 이전이 끝난 뒤에 테스트 묶음을 실행한다. 묶음이 바뀌었으면(`SUITE_HASH_MISMATCH`) 실행하지 않는다. 실행 시점과 데이터 정리 규칙은 D-10에서 정한다.

### 배포된 플랫폼과의 관계

배포된 플랫폼은 EC2 한 대에서 Docker Compose로 API, 워커, Temporal을 돌린다. 그 워커에는 Sandbox 클러스터(`SANDBOX_KUBECONFIG`)가 없어 Sandbox 루프는 scaffold만 실행된다. 실제 루프는 k3d가 있는 이 컴퓨터의 워커와 로컬 Temporal로 돌린다. 이 컴퓨터의 워커를 배포된 Temporal(SSM 포트 포워딩)에 붙이면 같은 Task Queue를 듣는 EC2 워커와 작업을 나눠 가지므로, 그렇게 하지 않는다.

데모 compose는 이 문서의 설정 이름(`ANALYSIS_*`, `SANDBOX_*`, `LLM_ENABLED`, `ANTHROPIC_API_KEY`)을 서버 `/opt/cmp/.env`에서 받도록 연결돼 있고, 비어 있으면 지금처럼 scaffold다(Provisioning 45, [데모 README](../../infra/demo/README.md#분석sandboxenv-실행-설정-180-티켓-45)). 실제 분석 Activity는 scaffold와 이름이 같아 한 워커는 한 경로만 등록한다. 서버 값과 경로 선택은 협의 대상이다.

## 9. 아직 없는 것

실제 AWS에서의 빌드 실행(S3·ECR·Docker, D-06·D-12·승인), 인계 후 실제 계획 게시 확인, `make smoke-temporal`, 승인 API·화면, 수정 패치의 push·PR 자동 생성(후순위), 원본 쪽 테스트 자원(있으면 AI 테스트의 추가 검증, D-09), 실제 Claude로 만든 AI 테스트의 품질(비용 승인 후 1회 확인). 목록과 이유는 [티켓 목록](tasks.md)의 결정 대기와 각 티켓을 따른다.
