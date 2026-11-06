# `.env` 참조(env_file_ref) 사용 가이드

대상: 분석 담당(ENV-05, 박승철님), 샌드박스 담당(ENV-06). 구현: ENV-03 [#147](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/147). 명세: [specs/env-file-ref/spec.md](../../specs/env-file-ref/spec.md).

사용자가 올린 `.env`는 플랫폼이 암호화해 한 번만 저장하고(ENV-01), 모든 단계는 참조 `env_file_ref: ContentRef{id, version, sha256}`만 주고받는다. 값을 읽는 함수는 `render_env` 하나뿐이다.

## 시작 요청에서 (Provisioning 44)

웹 시작 요청 `POST /migrations`(`target` 있음)는 `env_file_ref`가 필수다. 값은 `POST /env-files` 응답의 `env_file_ref.id`(`envf-<32 hex>`) 문자열이면 되고, API가 그 파일의 최신 버전으로 `{id, version, sha256}`를 고정한다(정확한 버전을 쓰려면 객체를 보낸다). 없으면 422 `ENV_FILE_REF_REQUIRED`, 없는·지운 파일은 `ENV_FILE_NOT_FOUND`·`ENV_FILE_EXPIRED`다. 환경변수가 없는 앱은 빈 `.env`를 올린다. 이후 단계는 아래처럼 고정된 참조만 받는다.

데모 서버(Provisioning 45): 저장소는 호스트 `/opt/cmp/data/env-files`(api·worker 모두 `/opt/cmp/env-files`, worker는 읽기만 하지만 저장소가 루트를 `chmod`하므로 읽기 전용 마운트는 쓰지 않는다), 키 `ENV_FILE_ENCRYPTION_KEY`는 `infra/demo/ensure-secrets.sh`가 `/opt/cmp/.env`에 없을 때만 만들고 바꾸지 않는다. 업로드는 `/connections`와 같은 운영자 키 `X-Enrollment-Admin-Key`다.

## 무엇을 어디서 부르나

| 누가 | 부를 것 | 위치 | 값을 읽나 |
| --- | --- | --- | --- |
| 분석(ENV-05) | `resolve_env_names(store, env_file_ref)` → 키 이름 | `cmp_providers.env_file` | 아니오 |
| 분석(ENV-05) | `build_env_plan(env_file_ref, env_names, code_env_reads)` → `EnvPlan` (편의 함수) | `cmp_providers.env_plan_builder` | 아니오 |
| 분석(ENV-05) | `to_env_exceptions(plan, db_resource_id=...)` → 기존 `EnvExceptions`(웹 화면) | `cmp_contracts.env_plan` | 아니오 |
| 샌드박스(ENV-06)·구축(ENV-04) | `render_env(store, plan, env_file_ref, db_outputs, app_release=...)` → `RenderedEnv` | `cmp_providers.env_file` | 예(PASS 키만) |

계약(`cmp_contracts.env_plan`)
- `EnvPlanEntry{key, disposition, binding?, reason?}`. `disposition`은 `PASS | EXCLUDED | REPLACED | UNREPRESENTABLE | MISSING`, `binding`은 `REPLACED`일 때만 있다.
- `EnvPlan{env_file_ref, entries}`. 키는 중복될 수 없다.
- `RenderedEnv{env: [EnvVarRef{name, secret_ref}], secrets: [RenderedSecret{name, data}], decisions: [EnvDecision{key, code}]}`.

## 분석 (ENV-05)

```python
from cmp_providers.env_file import resolve_env_names
from cmp_providers.env_plan_builder import build_env_plan
from cmp_contracts.env_plan import to_env_exceptions

names = resolve_env_names(store, env_file_ref)  # 값은 읽지 않는다
reads = sorted({s.target for s in code.call_sites if s.kind == "ENV_READ"})
plan = build_env_plan(env_file_ref, names + manifest_env_names, reads, has_database=db is not None)
env_exceptions = to_env_exceptions(plan, db_resource_id=db.id if db else None)
```

- `build_env_plan`은 `cmp_scanners.assembly`의 `_BINDING_PATTERNS`·`_DSN_VARIABLE`·`AWS_` 제외를 그대로 쓴다. 분석 모듈은 바꾸지 않았다. 같은 입력이면 `build_requirements`의 `env_exceptions`와 같은 값이 나온다(테스트로 확인).
- 합의 표는 최소 목록이다. 정규식은 접두사 `DB|DATABASE|POSTGRES(QL)|PG`(밑줄 선택)의 변형(예: `PG_HOST`, `POSTGRES_PASSWORD`)도 같은 바인딩으로 잡는다. 표와 다르게 잡히는 키가 문제라면 분석 쪽에서 규칙을 바꾸거나 이 함수를 쓰지 않아도 된다. 최종 규칙은 분석이 정한다.
- 우선순위: `EXCLUDED > REPLACED > MISSING > UNREPRESENTABLE > PASS`. DB가 없으면 DB 키는 `PASS`다(지금 분석과 같다).
- `MISSING`에는 기본값 필드가 없다. 코드에 기본값이 있어 문제가 없는 키는 `MISSING`으로 내지 않는다(`MISSING`은 모두 승인 전 차단된다).
- `reason`은 코드 값이다: `SOURCE_ONLY_CREDENTIAL`, `DB_OUTPUT_BINDING`, `NOT_DECLARED`, `DSN_FORMAT`.

## 샌드박스 (ENV-06)

바꿀 곳은 `cmp_worker.sandbox_deploy.SandboxService.prepare_sandbox`의 앱 루프 한 곳이다.

```python
# 지금
app_env_values[env_secret] = {
    **_env_values(env_file, [n for n in wanted if n not in extra]),
    **extra,
}

# 바꾼 뒤 (앱마다)
rendered = render_env(
    store, env_plan, env_file_ref, db_output_refs(db_release), app_release=releases[app.id]
)
if rendered.decisions:
    raise SandboxError(rendered.decisions[0].code)  # 기존 실패 분류 규칙대로
for secret in rendered.secrets:  # `<app release>-env`
    app_env_values[secret.name] = {**secret.reveal(), **extra}
replaced = {e.key: e.binding for e in env_plan.entries if e.disposition == "REPLACED"}
for item in rendered.env:
    if replaced.get(item.name) in {"password_secret_ref", "connection_url_secret_ref"}:
        # E4: 비밀번호·URL 키는 DB 출력 바인딩(구축의 `plan_publish._env_binding`과 같다)
        bindings[item.name] = {"resource_id": db.id, "output": replaced[item.name]}
    else:  # PASS(`-env`)와 host·port·name·username(`-conn`)
        bindings[item.name] = {"secret_ref": item.secret_ref.model_dump(mode="json")}
```

- 비밀번호·URL 키를 `{"secret_ref": ...}`로 넘기면 안 된다. 공통 렌더러가 플랫폼 관리 `<db>-url` Secret을 직접 참조하는 바인딩을 `SECRET_NAME_CONFLICT`로 막는다(티켓 19 U2). DB 출력 바인딩으로 넘겨도 최종 `secretKeyRef`는 같다(`DATABASE_URL` → `<db>-url`/`url`, `DB_PASSWORD` → `<db>-auth`/`password`).

- `db_output_refs(db_release)`는 샌드박스 표준 이름을 쓴다: `-conn`(host·port·name·username), `-auth`/`password`, `-url`/`url`.
- 새로 만들 Secret: `<db release>-conn`. 데이터는 `connection_secret_data(outputs)`(DB 출력 `host`·`port`·`name`·`username`)로 만든다. `-auth`·`-url`은 지금처럼 만든다.
- `REPLACED` `host`·`port`·`name`·`username` 키는 `ResourceOutputBinding`(평문 LITERAL) 대신 `-conn` Secret 키를 참조한다. `password_secret_ref`·`connection_url_secret_ref`는 DB 출력 바인딩 그대로다.

## 구축 (ENV-04)

결정 전체는 [ENV-04 착수 결정](../../tickets/env-file-ref/04-provisioning-render-env.md#착수-결정-2026-11-06).

- EnvPlan 입력: ENV-05 전까지 Planning 입력 등록 `PUT /provisioning/migrations/{id}/planning-input`의 선택 필드 `env_plan`(키 이름·처리 결과만). `env_plan.env_file_ref`는 `planning_request.environment.env_file_ref`와 같아야 한다. 없으면 지금처럼 사람이 만든 ConfigMap·Secret을 참조한다.
- Plan 게시: `preview_env(store, plan, ref, db_outputs, app_release=...)`로 키 이름만 읽어 `render_env`와 같은 `env`·`decisions`를 얻는다. `decisions`는 Preview `pending_decisions`(승인 불가)가 된다. EnvPlan은 SPEC 산출물에 들어가 Digest로 고정된다.
- 실행: 대상 DB 출력 `database_outputs(release, namespace, name, username, password_secret_ref)` → `DbOutputRefs.from_outputs(outputs, database_release=release)` → `render_env` → `deployment_secrets(rendered, outputs, database_release=release)`(`<app>-env`, 참조될 때 `<db>-conn`). AWS/GCP는 배포 Activity가 stdin으로, ON_PREM은 Agent가 `GET /agents/{agent_id}/jobs/{job_id}/env-secrets`로 받아 클러스터 안에서 적용한다. 둘 다 플랫폼 관리 라벨이 없는 같은 이름 Secret은 덮어쓰지 않는다(`SECRET_NAME_CONFLICT`).
- 샌드박스와 구축의 `render_env` 결과는 `REPLACED` 참조만 다르다(테스트로 확인). `-auth`·`-url`은 기존 단계가 그대로 만든다. 같은 EnvPlan이면 두 경로의 비밀번호·URL 최종 `secretKeyRef`도 같다(`tests/sandbox/test_env_render.py`).

| 코드(구축) | 뜻 |
| --- | --- |
| `ENV_PLAN_REF_MISMATCH` | 등록·실행 시 EnvPlan 참조가 Planning `env_file_ref`·Values `config_ref`와 다름 |
| `ENV_PLAN_BINDING_MISMATCH` | 요구사항 `env_bindings`가 EnvPlan의 REPLACED·binding과 다름(게시 안 함) |
| `ENV_FILE_STORE_UNCONFIGURED` | 워커·API에 `.env` 저장소 설정 없음 |
| `ENV_FILE_UNAVAILABLE` | 게시 시 저장소 오류(이슈에 `ENV_FILE_EXPIRED` 등) |
| `ENV_BINDING_MISMATCH` | 실행 시 렌더 결과가 승인된 Values 참조와 다름 |
| `ENV_SECRETS_UNAVAILABLE`·`ENV_SECRETS_INVALID`·`ENV_SECRETS_NOT_CONFIGURED` | Agent가 Secret을 받지 못함·이름/키 불일치·이전 실행기 |

## 결정(decisions)과 오류

`render_env`는 아래가 하나라도 있으면 값을 읽지 않거나 버리고 `env`·`secrets`를 비운 채 `decisions`만 돌려준다. 호출하는 쪽은 구조화 코드로 실패한다(승인 전 차단, 착수 결정 D1).

| 코드 | 뜻 |
| --- | --- |
| `ENV_UNREPRESENTABLE` | `UNREPRESENTABLE`(예: `DB_DSN`) |
| `ENV_MISSING` | `MISSING`(코드는 읽지만 선언 안 됨) |
| `ENV_VALUE_MISSING` | `PASS`인데 `.env`에 값이 없음 |
| `ENV_DB_OUTPUT_MISSING` | `REPLACED`인데 `db_outputs`가 없음 |

`EnvFileError(code)`: `ENV_FILE_NOT_FOUND`, `ENV_FILE_DIGEST_MISMATCH`, `ENV_FILE_EXPIRED`(저장소), `ENV_PLAN_REF_MISMATCH`(`env_plan.env_file_ref`와 인자가 다름). 메시지는 코드뿐이다.

## 값 비노출

- `RenderedSecret.data`는 `SecretStr`이다. `repr`·`str`·`model_dump`·JSON·`canonical_bytes()`에는 `**********`만 나온다. 값은 클러스터에 적용하기 직전에 `reveal()`로만 꺼낸다.
- `RenderedEnv`를 Temporal 입력·결과·이력, 이벤트, 로그로 보내지 않는다. Activity 안에서 렌더하고 바로 적용한다(직렬화하면 값이 `**********`로 바뀐다).
- 결과는 키 순서로 정렬된다. 같은 입력이면 `canonical_bytes()`가 같다.

## 테스트에서 InMemory 저장소 쓰기

```python
from cmp_providers.env_file import InMemoryEnvFileStore, resolve_env_names

store = InMemoryEnvFileStore()
ref = store.put(b"DB_HOST=old\nAPP_MODE=prod\n")  # ContentRef(id="env-1", version="1", sha256=…)
assert resolve_env_names(store, ref) == ["APP_MODE", "DB_HOST"]
assert store.read_values_calls == 0  # 분석이 값을 읽지 않았음을 확인
store.expire(ref)  # 이후 읽기는 ENV_FILE_EXPIRED
```

- 파싱 규칙은 `cmp_scanners.envfile.parse_env_names`와 같다(주석·빈 줄·`export`). 값은 양끝 같은 따옴표를 벗기고, 중복 키는 마지막이 이긴다.
- 같은 `file_id`로 다시 `put`하면 `version`이 올라간다.
- 실제 저장소(ENV-01)는 같은 `EnvFileStore` Protocol(`read_names`, `read_values`)을 구현한다. 테스트는 InMemory로 시작하고 교체하면 된다.

## 전달 메시지 초안

박승철님(분석, ENV-05)

> 승철님, `.env` 공통 함수(ENV-03, #147)가 준비됐습니다. 분석에서는 `cmp_providers.env_file.resolve_env_names(store, env_file_ref)`로 키 이름만 읽고(값은 안 읽습니다), `cmp_providers.env_plan_builder.build_env_plan(...)`으로 EnvPlan을 만든 뒤 `cmp_contracts.env_plan.to_env_exceptions(plan, db_resource_id=...)`로 기존 EnvExceptions를 계속 내시면 됩니다. 빌더는 `assembly`의 `_BINDING_PATTERNS`·DSN·`AWS_` 규칙을 그대로 써서 지금 결과와 같은 값이 나오고(테스트로 확인), 분석 모듈은 건드리지 않았습니다. 최종 규칙은 승철님이 정하시면 되고, 합의 표 외에 `PG_HOST` 같은 접두사 변형도 잡힌다는 점만 확인 부탁드립니다. `MISSING`은 승인 전 차단이라, 코드에 기본값이 있는 키는 `MISSING`으로 내지 말아 주세요. 사용법과 테스트용 InMemory 저장소는 `docs/engineering/env-file-guide.md`에 있습니다.

샌드박스 담당(ENV-06)

> 안녕하세요, `.env` 공통 함수(ENV-03, #147)가 준비됐습니다. 샌드박스는 `sandbox_deploy.prepare_sandbox`에서 `_env_values(env_file, …)` 한 곳을 `render_env(store, env_plan, env_file_ref, db_output_refs(db_release), app_release=…)`로 바꾸시면 됩니다. 결과의 `secrets`(`<app release>-env`)는 `reveal()`로 값을 꺼내 지금처럼 Secret을 만들고, `env`는 `secret_ref`로 바인딩에 넣으면 됩니다. `decisions`가 있으면 그 코드로 실패 처리해 주세요(`DB_DSN`·MISSING은 승인 전 차단으로 정했습니다). 하나 새로 생기는 것은 `<db release>-conn` Secret(host·port·name·username)이고, DB 키도 평문 대신 이 Secret을 참조합니다. 데이터는 `connection_secret_data(outputs)`로 만들 수 있습니다. 예시 코드와 InMemory 저장소 사용법은 `docs/engineering/env-file-guide.md`에 있습니다.
