# ENV-06 — 샌드박스: env_file 경로 대신 render_env 사용

## 담당
샌드박스 담당

## 상태
DRAFT — 담당자 검토 전.

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
- [ ] 샌드박스가 `env_file_ref`로 앱 env를 만들고 워커 로컬 파일이 필요 없다.
- [ ] 같은 입력에서 구축(ENV-04)과 `PASS`·`EXCLUDED` 결과가 같고 DB 출력만 다르다.
- [ ] 원문 값이 Sandbox 로그·이벤트·Temporal 이력에 없다.

## Test Seam
기존 샌드박스 테스트 + InMemory `.env` 저장소.

## 결정 필요
- 1. `UNREPRESENTABLE`·기본값 없는 `MISSING`을 샌드박스에서 실패로 볼지, 사람이 값을 넣게 할지(구축과 같은 정책을 쓴다)
