# ENV-03 — 공통 함수 resolve_env_names·render_env

## 담당
민서 (분석·샌드박스·구축이 가져다 쓴다)

## 상태
IN_PROGRESS — GitHub Issue [#147](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/147)

## 목적
`.env` 참조를 해석하는 함수를 한 곳에 두어, 분석은 키 이름만, 샌드박스와 구축은 같은 규칙으로 env·Secret을 만든다.

## 관련 요구사항
ENV-005, ENV-006(계약), ENV-007, ENV-008. PROV-005, PROV-012, PROV-021.

## 선행 작업
ENV-01과 저장소 인터페이스 합의(이 티켓의 InMemory 구현으로 먼저 개발 가능).

## 범위
- 포함
  - 계약 `cmp_contracts.env_plan`
    - `EnvPlanEntry{key, disposition: PASS|EXCLUDED|REPLACED|UNREPRESENTABLE|MISSING, binding?: host|port|name|username|password_secret_ref|connection_url_secret_ref, reason?}`
    - `EnvPlan{env_file_ref, entries[]}`와 `EnvExceptions` 계산 함수(웹 호환)
    - `RenderedEnv{env[], secrets[], decisions[]}`, `EnvDecision{key, code}`
  - `EnvFileStore` 프로토콜(`read_names`, `read_values`)과 `InMemoryEnvFileStore`
  - `resolve_env_names(env_file_ref) -> list[str]`
  - `render_env(env_plan, env_file_ref, db_outputs) -> RenderedEnv`
    - `PASS`: 값을 `<앱 release>-env` Secret에 넣고 `secretKeyRef`
    - `REPLACED`: DB 출력 Secret의 `binding` 키에 `secretKeyRef`
    - `EXCLUDED`: 제외
    - `UNREPRESENTABLE`·기본값 없는 `MISSING`: `decisions`로 반환, `env`·`secrets` 비움
  - 결과의 repr·직렬화에는 값이 없다(Secret 값은 실행 주체가 적용할 때만 꺼낸다).
  - 단위 테스트, SDD 갱신
- 제외: 실제 클러스터 적용(호출하는 쪽), 저장소 암호화 구현(ENV-01)

## 수용 기준
- [x] 분류별 결과가 명세 표와 같다(`DB_HOST`→`host` 참조, `DB_PASSWORD`→`password_secret_ref`, `DATABASE_URL`→`connection_url_secret_ref`, `DB_DSN`→결정 필요).
- [x] 같은 입력이면 결과 바이트가 같다.
- [x] `resolve_env_names`는 값을 읽지 않는다(InMemory 저장소에서 `read_values` 호출 0회).
- [x] 결과·repr·예외·로그에 값이 없다.
- [x] `EnvExceptions`가 EnvPlan에서 기존 분석 결과와 같은 값으로 계산된다.
- [x] 샌드박스·구축 DB 출력만 바꾼 두 호출에서 `PASS`·`EXCLUDED` 결과가 같다.

## 구현 위치
- 계약: `packages/contracts/src/cmp_contracts/env_plan.py`
- 저장소 Protocol·InMemory·`resolve_env_names`·`render_env`·DB 연결 Secret: `packages/providers/src/cmp_providers/env_file.py`
- 분석용 빌더: `packages/providers/src/cmp_providers/env_plan_builder.py`
- 테스트: `tests/test_env_file.py`
- 사용 가이드·전달 메시지: `docs/engineering/env-file-guide.md`

## Test Seam
순수 함수 + InMemory 저장소. 외부 I/O 없음.

## 착수 결정 (2026-11-05, 사용자 승인)

| # | 항목 | 결정 |
| --- | --- | --- |
| D1 | 결정 필요 1번: `UNREPRESENTABLE`·기본값 없는 `MISSING` | 승인 전에 막는다. `render_env`는 이들을 `decisions`로 돌려주고 `env`·`secrets`는 비운다(값도 읽지 않는다). 호출하는 쪽은 구조화 코드로 실패한다. 사람이 값을 넣는 입력은 두지 않는다. |
| D2 | 저장·보관 | 저장 구현·암호화·보관 기간은 ENV-01 몫이다. 이 티켓은 읽기 인터페이스 `EnvFileStore{read_names, read_values}`와 오류 코드 `ENV_FILE_NOT_FOUND`·`ENV_FILE_DIGEST_MISMATCH`·`ENV_FILE_EXPIRED`만 정한다. ENV-01은 이 Protocol을 구조적으로 구현한다. |
| D3 | `host`·`port`·`name`·`username` 참조 방식 | 평문 env가 아니라 DB 연결 Secret `<DB release>-conn`의 키 `host`·`port`·`name`·`username`을 `secretKeyRef`로 참조한다. 비밀번호는 기존 `password_secret_ref`(샌드박스 `<DB release>-auth`/`password`), URL은 기존 `<DB release>-url`/`url`이다. `-conn` Secret은 아직 아무도 만들지 않는다. ENV-04(구축)·ENV-06(샌드박스)이 DB 출력에서 `connection_secret_data(...)`로 만들어 적용한다. |
| D4 | `MISSING`의 기본값 | P0 EnvPlan에는 기본값 필드가 없다. 따라서 `MISSING`은 모두 "기본값 없음"으로 보고 `decisions`가 된다. 코드에 기본값이 있어 문제가 없는 키는 분석이 `MISSING`으로 내지 않는다(ENV-05에서 확인). |
| D5 | 렌더 결과의 값 보관 | `RenderedSecret.data`는 `SecretStr`이다. `repr`·`model_dump`·JSON에는 `**********`만 나온다. 값은 실행 주체가 `reveal()`로 적용 직전에만 꺼낸다. `RenderedEnv`는 Temporal 경계를 넘기지 않는다(Activity 안에서 렌더하고 바로 적용). |
| D6 | `render_env` 시그니처 | `render_env(store, env_plan, env_file_ref, db_outputs, *, app_release)`. 앱이 여럿이면 앱마다 부른다(`<app release>-env`). `env_plan.env_file_ref`와 인자가 다르면 `EnvFileError("ENV_PLAN_REF_MISMATCH")`. |
| D7 | 추가 결정 코드 | `ENV_UNREPRESENTABLE`(UNREPRESENTABLE), `ENV_MISSING`(MISSING), `ENV_VALUE_MISSING`(PASS인데 파일에 값이 없음), `ENV_DB_OUTPUT_MISSING`(REPLACED인데 `db_outputs` 없음). |
| D8 | EnvPlan 빌더 | `cmp_providers.env_plan_builder.build_env_plan`은 분석용 편의 함수다. `cmp_scanners.assembly`의 `_BINDING_PATTERNS`·`_DSN_VARIABLE`·`AWS_` 제외를 그대로 쓰고 분석 모듈은 고치지 않는다. 합의 표는 최소 목록이고 정규식은 접두사 변형도 잡는다. 최종 규칙은 분석(ENV-05)이 정한다. |
| D9 | `EnvExceptions.replaced` 값 | 기존 형식 `<db resource id>.<binding>`을 유지하려고 `to_env_exceptions(plan, db_resource_id=...)`로 DB 리소스 ID를 받는다(EnvPlan에는 리소스 ID가 없다). |

## 검증 (2026-11-05)
- `tests/test_env_file.py` 38개 통과(분류별 결과, 합의 바인딩 표, `DB_DSN`→`UNREPRESENTABLE`, `read_values` 0회, repr·직렬화·로그·예외 비노출, 결과 바이트 동일, `build_requirements`와 `EnvExceptions` 일치, 샌드박스·대상 DB 출력 차이는 `REPLACED` 참조뿐).
- `make check` 통과(1683 passed, 8 skipped). HTTP·Temporal 변경 없음(`make openapi`·`make smoke-temporal` 해당 없음).
- 상태는 조정 세션 검토 전까지 IN_PROGRESS로 둔다.
