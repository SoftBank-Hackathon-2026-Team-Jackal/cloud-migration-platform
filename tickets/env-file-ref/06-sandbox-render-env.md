# ENV-06 — 샌드박스: env_file 경로 대신 render_env 사용

## 담당
샌드박스 담당

## 상태
DONE — PR #198 병합, Issue #197 (ENV-05·분석 26과 한 PR)

## 목적
샌드박스가 워커 로컬 파일(`SandboxSettings.env_file`) 대신 구축과 같은 `env_file_ref`·EnvPlan·`render_env`로 앱 env를 만든다. 샌드박스와 구축의 차이를 DB 출력 하나로 줄인다.

## 관련 요구사항
ENV-007, ENV-008. 샌드박스 검증 요구사항.

## 선행 작업
ENV-03(`render_env`), ENV-05(EnvPlan).

## 현재
`sandbox_deploy.prepare_sandbox`가 `_env_values(env_file, names)`로 워커 로컬 파일에서 값을 읽고, 바인딩되지 않은 키를 전부 `<release>-env` Secret에 넣는다.

## 범위
- 포함
  - `render_env(EnvPlan, env_file_ref, 샌드박스 DB 출력)` 결과로 Secret과 env 참조를 만든다.
  - `SandboxSettings.env_file`은 `env_file_ref`가 없을 때만 쓰는 호환 경로로 남기거나 제거한다(담당자 결정).
  - `decisions`(결정 필요)가 있으면 기존 실패 분류(SPEC 등) 규칙대로 처리한다.
- 제외: 공통 함수 구현(ENV-03), 구축(ENV-04)

## 수용 기준
- [x] 샌드박스가 `env_file_ref`로 앱 env를 만들고 워커 로컬 파일이 필요 없다.
- [x] 같은 입력에서 구축(ENV-04)과 `PASS`·`EXCLUDED` 결과가 같고 DB 출력만 다르다.
- [x] 원문 값이 Sandbox 로그·이벤트·Temporal 이력에 없다.
- [x] 앱 env 바인딩이 ENV-04 E4를 따른다: PASS → `<app>-env`, REPLACED host·port·name·username → `<db>-conn`, REPLACED `password_secret_ref`·`connection_url_secret_ref` → DB 출력 바인딩(`{"resource_id", "output"}`). 최종 `secretKeyRef`가 구축과 같다.

## 수정 기록 (2026-11-07)
- 현상: 데모 실제 실행에서 `.env`(DATABASE_URL 포함)를 올리면 `sbx_apply`가 `resources.1.env_bindings.<env>: SECRET_NAME_CONFLICT`로 실패했다.
- 원인: `_render_app_env`가 모든 키를 Secret 참조로 넘겨 DATABASE_URL이 플랫폼 관리 `<db>-url` Secret을 직접 참조했다. 공통 렌더러는 이를 막는다(티켓 19 U2, 의도된 보호).
- 수정: 비밀번호·URL 키만 DB 출력 바인딩으로 넘긴다(구축 `plan_publish._env_binding`과 같은 규칙). 공통 렌더러 검사·운영 경로·Secret 생성(`-auth`·`-url`·`-conn`·`-env`)은 그대로다.

## Test Seam
기존 샌드박스 테스트 + InMemory `.env` 저장소.

## 결정 필요
- 1. `UNREPRESENTABLE`·기본값 없는 `MISSING`을 샌드박스에서 실패로 볼지, 사람이 값을 넣게 할지(구축과 같은 정책을 쓴다)
