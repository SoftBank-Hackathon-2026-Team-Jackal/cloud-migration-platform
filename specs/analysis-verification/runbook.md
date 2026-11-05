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
| 앱 이미지 | 이미 빌드·푸시된 이미지(Digest 고정). 실제 Build(D-12)가 정해지기 전에는 이것으로 대신한다. 레지스트리가 비공개면 k3d가 받을 수 있게 공개하거나 pull 자격 증명을 넣어야 한다 |

## 2. 설정 (`.env`, 값은 예시)

`.env.example`의 항목을 `.env`로 복사해 채운다. 분석 결과 저장소는 **분석 워커와 Sandbox 워커, Planning이 같은 디렉터리**를 가리키게 한다. Planning은 요구사항을 `<저장소>/<SHA-256>`로 읽는다.

```bash
ANALYSIS_ARTIFACT_STORE_DIR=$HOME/cmp-data/store
ARTIFACT_STORE_DIR=$HOME/cmp-data/store          # Planning이 읽는 곳(같은 값)
ANALYSIS_WORKSPACE_ROOT=$HOME/cmp-data/workspaces
SANDBOX_KUBECONFIG=$HOME/.kube/cmp-sandbox.yaml
SANDBOX_PREBUILT_IMAGES={"orders-api": "ghcr.io/<소유자>/<앱>@sha256:<digest>"}
LLM_ENABLED=true
LLM_LEDGER_PATH=$HOME/cmp-data/llm-ledger.json
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

## 6. 결과 확인과 정리

- 부모 Query `status`의 `pipeline`에 분석·요구사항 참조와 발행 결과가 있다. 종료 상태는 `scaffold_complete`(통과), `failed`(원인은 `failure_code`), `cancelled`다.
- 발행된 최종 분석 버전·요구사항·패치 산출물은 저장소 디렉터리에 해시 이름 파일로 있다.
- Sandbox 네임스페이스(`cmp-sbx-*`)는 통과·실패·취소 모두 워커가 지운다. 남았다면 `kubectl --kubeconfig $SANDBOX_KUBECONFIG --context k3d-cmp-sandbox get ns`로 찾아 지운다(라벨 `cmp.sandbox/expires-at`이 만료 시각이다).

## 7. 자주 막히는 곳

| 증상 | 원인과 조치 |
| --- | --- |
| 단계가 `sbx_build`에서 실패, 코드 `BUILD_NOT_CONFIGURED` | `SANDBOX_PREBUILT_IMAGES`가 비어 있다. 앱마다 Digest 고정 이미지를 적는다 |
| `PREBUILT_IMAGE_NOT_DIGEST_PINNED` | 태그(`:latest`)는 거절한다. `@sha256:` 형태로 적는다 |
| `LLM_DISABLED`, `BUDGET_RUN/DAY/MONTH` | 모델이 꺼졌거나 한도에 닿았다. 정상 동작이며 사람 검토로 넘어간다 |
| Planning이 요구사항을 못 읽음 | `ARTIFACT_STORE_DIR`와 `ANALYSIS_ARTIFACT_STORE_DIR`가 다르다 |
| 앱이 시작하지 않음 | 첫 시도는 포트·헬스 경로가 틀릴 수 있고, 이 경우 SPEC으로 분류되어 Fixer가 고친다 |

## 8. 아직 없는 것

실제 Build(S3·ECR·Docker, D-12), Planning 인계(티켓 15), 승인 API·화면, 만료된 Sandbox의 주기적 정리 작업. 목록과 이유는 [티켓 목록](tasks.md)의 결정 대기와 각 티켓을 따른다.
