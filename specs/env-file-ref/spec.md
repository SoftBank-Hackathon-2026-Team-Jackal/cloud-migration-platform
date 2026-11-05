# Feature: .env 단일 참조(env_file_ref)와 공통 환경변수 해석

## Goal

사용자가 웹에서 올린 `.env` 하나를 플랫폼이 암호화해 한 번만 저장하고, 그 참조(`env_file_ref`)를 분석·샌드박스·구축이 함께 쓴다. 분석은 키 이름만 읽어 키마다 처리 규칙(EnvPlan)을 정하고, 샌드박스와 구축은 같은 공통 함수(`render_env`)로 같은 EnvPlan과 `env_file_ref`를 Kubernetes env와 Secret으로 바꾼다. 두 환경의 차이는 DB 출력(대상 DB 주소·Secret) 하나뿐이다.

## 현재 상태 (2026-11-05, origin/main 기준)

- 시작 API `POST /migrations`(`cmp_contracts.migration.MigrationRequest`)는 `project_id`·`source_provider`·`target_provider`만 받는다. `.env`를 넘길 길이 없다.
- 분석: `AnalysisInput`에 env 필드가 없다. `cmp_scanners.source.fetch_source`가 클론한 저장소 안의 `.env*` 파일에서 키 이름만 읽는다(`_env_names`). `AnalysisRequest.env_file_ref: ContentRef`는 정의만 있고 쓰이지 않는다. 분류 결과는 `EnvExceptions{excluded, replaced, missing}`이고, 웹(`cloud-migration-web` b53869d)이 이 값을 화면에 쓴다.
- 샌드박스: `SandboxSettings.env_file`(워커 로컬 파일 경로)에서 값을 읽는다(`sandbox_deploy._env_values`).
- 구축(v2): 계약은 `EnvironmentInputV2.env_file_ref: ContentRef`로 받는다(`provisioning_v2`). Plan 게시(`plan_publish`)는 이 참조를 앱 Values의 `config_ref`로 넘길 뿐, 값을 대상에 만들지 않는다. 대상에 ConfigMap·Secret이 미리 없으면 `CONFIG_MAP_MISSING`·`USER_SECRET_MISSING`·`SECRET_REFERENCE_MISSING`으로 실패한다(Provisioning 티켓 20).
- 웹: 사용자가 화면에서 `.env`를 올리지만, 브라우저가 키 이름만 읽고 값은 버린다. 시작 요청의 `env_file_ref`는 비어 있다.

## In scope / out of scope

포함
- `.env` 업로드 API, 암호화 저장, `env_file_ref` 반환, 삭제 정책
- 시작 요청 `MigrationRequest.env_file_ref`와 분석·샌드박스·구축 입력 전달
- 공통 함수 `resolve_env_names`, `render_env`와 테스트용 InMemory 저장소
- 분석의 EnvPlan 출력, 샌드박스·구축의 `render_env` 전환

제외
- 웹 업로드 화면 자체(웹 담당, 이 명세의 API를 쓴다)
- 외부 Secret Manager 연동, `.env` 이외 형식(YAML·JSON 설정 파일)
- 앱 코드가 런타임에 파일로 읽는 `.env`(파일 마운트)
- Fixer가 새 변수를 추가하는 처리(ENV-07, 담당·방식 미정)

## 합의된 규칙 (분석 담당 박승철님 답변, 2026-11-05)

키 분류
- `EXCLUDED`: 원본 환경 전용. 대상에 복사하지 않는다(예: AWS 자격 증명 `AWS_*`).
- `REPLACED`: 대상 DB 출력에 바인딩한다. 원본 값은 복사하지 않는다.
- `MISSING`: 코드는 읽지만 `.env`에 선언되지 않았다.
- `UNREPRESENTABLE`: 바인딩 계약으로 표현할 수 없다(예: `DB_DSN`처럼 key=value 형식). `REPLACED`에 넣지 않고 보고한다.
- `PASS`: 위에 해당하지 않는 나머지. 값을 바꾸지 않고 그대로 전달하며, ConfigMap이 아니라 Secret에 넣는 것이 기본이다.

`REPLACED` 바인딩

| `.env` 키 | 바인딩(DB 출력) |
| --- | --- |
| `DB_HOST`, `PGHOST` | `host` |
| `DB_PORT` | `port` |
| `DB_NAME`, `POSTGRES_DB`, `PGDATABASE` | `name` |
| `DB_USER`, `DB_USERNAME` | `username` |
| `DB_PASSWORD` | `password_secret_ref` |
| `DATABASE_URL`, `DB_URI` | `connection_url_secret_ref` |

위 표는 합의된 최소 목록이다. 현재 분석 코드(`cmp_scanners.assembly._BINDING_PATTERNS`)는 접두사 `DB|DATABASE|POSTGRES(QL)|PG`(밑줄 선택)의 변형도 같은 바인딩으로 본다. 구현은 이 패턴을 그대로 쓰고, 표와 다르게 잡히는 키가 있으면 ENV-05에서 박승철님과 확인한다.

바인딩된 변수는 값을 주입하지 않고 `secretKeyRef` 같은 참조로만 연결한다. `host`·`port`·`name`·`username`도 DB 출력 Secret의 키로 참조한다(평문 env 값으로 넣지 않는다). 이 키들은 DB 연결 Secret `<DB release>-conn`(키 `host`·`port`·`name`·`username`)에 둔다. 비밀번호는 `password_secret_ref`(샌드박스 `<DB release>-auth`/`password`), URL은 `<DB release>-url`/`url`이다(ENV-03 D3).

## Requirements

ENV-001 업로드와 단일 저장
- 웹 → 웹 서버(BFF) → 플랫폼 API로 `.env`를 한 번 업로드한다. 플랫폼은 원본 바이트를 암호화해 하나만 저장하고 바꾸지 않는다. 응답은 `env_file_ref: ContentRef{id, version, sha256}`다(`sha256`은 원본 바이트 기준).
- 같은 이전에서 다시 올리면 새 `version`이 된다. 이미 승인된 Plan은 승인 때의 `version`·`sha256`만 쓴다.

ENV-002 비밀 보호 (PROV-021)
- 원문 값은 API 응답·로그·Temporal 입력과 이력·Plan·Preview·이벤트·운영 로그에 남기지 않는다. 키 이름과 참조만 남는다.
- 업로드 API는 운영자 키로 보호하고 웹 서버에서만 호출한다(브라우저 직접 호출 없음).

ENV-003 삭제
- 원본은 그 `.env`를 쓴 이전이 끝난 뒤 7일에 삭제한다. 이전에 쓰이지 않은 업로드는 마지막 업로드 뒤 7일. `DELETE /env-files/{id}`로 즉시 지울 수도 있다(결정 2, 2026-11-05).
- 삭제 뒤 `env_file_ref`를 읽으면 구조화 오류(`ENV_FILE_EXPIRED`)를 낸다.

ENV-004 전달
- `MigrationRequest`에 선택 필드 `env_file_ref`를 추가한다. 없으면 지금처럼 동작한다.
- Migration Workflow가 이 참조를 분석·샌드박스·구축 입력으로 넘긴다. 값은 넘기지 않는다.

ENV-005 키 이름 해석
- `resolve_env_names(env_file_ref) -> list[str]`는 키 이름만 돌려준다. 값을 메모리 밖으로 내보내지 않는다.

ENV-006 처리 규칙(EnvPlan)
- 분석은 `env_file_ref`의 키 이름과 코드가 읽는 키를 대조해 키마다 `EnvPlanEntry{key, disposition, binding?, reason?}`를 낸다.
  - `disposition`: `PASS | EXCLUDED | REPLACED | UNREPRESENTABLE | MISSING`
  - `binding`: `host | port | name | username | password_secret_ref | connection_url_secret_ref` (`REPLACED`일 때만)
  - `reason`: 코드 값(예: `SOURCE_ONLY_CREDENTIAL`, `DSN_FORMAT`). 문구가 아니다.
- 기존 `EnvExceptions{excluded, replaced, missing}`는 EnvPlan에서 계산할 수 있어야 한다(웹 화면 호환).

ENV-007 렌더(공통 함수)
- `render_env(env_plan, env_file_ref, db_outputs) -> RenderedEnv{env, secrets, decisions}`
  - `PASS`: 원본 값을 그 환경의 앱 env Secret(`<release>-env`)에 넣고 `secretKeyRef`로 연결
  - `REPLACED`: 그 환경 DB 출력 Secret의 키(`binding`)에 `secretKeyRef`로 연결
  - `EXCLUDED`: 넣지 않음
  - `UNREPRESENTABLE`, 기본값 없는 `MISSING`(P0에는 기본값 필드가 없어 `MISSING` 전부): 렌더를 진행하지 않고 `decisions`로 돌려줌. `env`·`secrets`는 비운다
  - 실제 시그니처는 `render_env(store, env_plan, env_file_ref, db_outputs, *, app_release)`이고 앱마다 부른다. 사용법: [env-file-guide](../../docs/engineering/env-file-guide.md)
- 같은 입력이면 결과 바이트가 같다(Digest 고정). Secret 값은 결과 객체 안에만 있고, 직렬화·로그용 표현에는 값이 없다.

ENV-008 같은 규칙, 다른 DB 출력
- 샌드박스와 구축은 같은 `env_file_ref`와 같은 EnvPlan을 쓴다. 두 호출의 차이는 `db_outputs`(샌드박스 DB 또는 대상 DB)뿐이다.

## Acceptance criteria

- 업로드한 `.env`가 암호화되어 한 번만 저장되고, `env_file_ref`로만 참조된다.
- 원문 값이 응답·로그·Temporal 이력·Plan·Preview·이벤트에 없다(테스트로 문자열 검사).
- 분석이 저장소 파일이 아니라 `env_file_ref`에서 키 이름을 읽어 EnvPlan을 낸다. `DB_DSN`류는 `UNREPRESENTABLE`이다.
- 샌드박스와 구축이 같은 `env_file_ref`·EnvPlan으로 `render_env`를 부르고, DB 출력만 다르다.
- 사람이 대상 클러스터에 ConfigMap·Secret을 미리 만들지 않아도 구축(09·11·12)이 성공한다.
- `UNREPRESENTABLE`·기본값 없는 `MISSING`은 결정 필요 1번의 정책대로 처리된다.

## Open questions

1. ~~`UNREPRESENTABLE`(`DB_DSN`)과 기본값 없는 `MISSING`을 승인 전에 막을지, 사람이 값을 넣게 할지.~~ 결정(2026-11-05): 승인 전에 막는다. `render_env`는 `decisions`만 돌려주고 호출하는 쪽이 구조화 코드로 실패한다(ENV-03 D1).
2. ~~원본 `.env` 보관 기간~~ → 확정(2026-11-05): 이전 종료 7일 뒤 삭제 + 명시적 `DELETE`. [ENV-01](../../tickets/env-file-ref/01-env-upload-store.md).
3. ~~저장 위치~~ → 확정(2026-11-05): 플랫폼 디렉터리의 파일 저장(0600) + AES-256-GCM, 키는 플랫폼 설정 `ENV_FILE_ENCRYPTION_KEY`. [ENV-01](../../tickets/env-file-ref/01-env-upload-store.md).
4. Fixer가 새 변수를 추가할 때 EnvPlan에 `ADDED`로 넣을지, 담당은 누구인지(ENV-07).
