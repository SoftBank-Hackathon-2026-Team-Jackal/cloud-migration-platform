# ENV-03 — 공통 함수 resolve_env_names·render_env

## 담당
민서 (분석·샌드박스·구축이 가져다 쓴다)

## 상태
DRAFT

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
- [ ] 분류별 결과가 명세 표와 같다(`DB_HOST`→`host` 참조, `DB_PASSWORD`→`password_secret_ref`, `DATABASE_URL`→`connection_url_secret_ref`, `DB_DSN`→결정 필요).
- [ ] 같은 입력이면 결과 바이트가 같다.
- [ ] `resolve_env_names`는 값을 읽지 않는다(InMemory 저장소에서 `read_values` 호출 0회).
- [ ] 결과·repr·예외·로그에 값이 없다.
- [ ] `EnvExceptions`가 EnvPlan에서 기존 분석 결과와 같은 값으로 계산된다.
- [ ] 샌드박스·구축 DB 출력만 바꾼 두 호출에서 `PASS`·`EXCLUDED` 결과가 같다.

## Test Seam
순수 함수 + InMemory 저장소. 외부 I/O 없음.

## 결정 필요
- 1. `UNREPRESENTABLE`·기본값 없는 `MISSING`을 렌더 단계에서 오류로 막을지, 사람이 넣은 값을 받을 입력을 둘지(`render_env` 시그니처에 영향)
