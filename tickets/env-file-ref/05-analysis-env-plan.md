# ENV-05 — 분석: env_file_ref에서 키 읽기·EnvPlan 출력

## 담당
박승철님 (분석)

## 상태
DONE — PR #198 병합, Issue #197 (ENV-06·분석 26과 한 PR)

## 목적
분석이 클론한 저장소 안의 `.env*` 파일 대신 사용자가 올린 `.env`(`env_file_ref`)에서 키 이름을 읽고, 코드가 읽는 키와 대조해 키마다 처리 규칙(EnvPlan)을 낸다.

## 관련 요구사항
ENV-005, ENV-006. 분석 명세의 환경변수 분류(AV 요구사항, `EnvExceptions`).

## 선행 작업
ENV-02(입력 전달), ENV-03(`resolve_env_names`, EnvPlan 계약).

## 현재
`cmp_scanners.source.fetch_source`가 저장소 안 `.env*`에서 키 이름을 읽는다(`_env_names`). `cmp_scanners.assembly.build_requirements`가 `_BINDING_PATTERNS`로 `EnvExceptions{excluded, replaced, missing}`를 만든다. `DB_DSN`은 `_DSN_VARIABLE`로 따로 잡는다.

## 범위
- 포함
  - `AnalysisInput.env_file_ref`가 있으면 `resolve_env_names(env_file_ref)`로 키 이름을 읽는다. 없으면 기존 저장소 파일 방식을 유지한다.
  - 키마다 `EnvPlanEntry`를 낸다. 분류 규칙은 [명세의 합의 규칙](../../specs/env-file-ref/spec.md#합의된-규칙-분석-담당-박승철님-답변-2026-11-05)과 같다.
  - `DB_DSN`처럼 key=value 형식은 `UNREPRESENTABLE`(`reason: DSN_FORMAT`)로 내고 `REPLACED`에 넣지 않는다.
  - EnvPlan을 분석 산출물로 저장하고 참조를 다음 단계로 넘긴다. 기존 `EnvExceptions`도 계속 낸다(웹 화면 호환).
- 제외: 값 읽기(분석은 값을 보지 않는다), 렌더(ENV-03), 샌드박스 적용(ENV-06)

## 수용 기준
- [x] `env_file_ref`가 있으면 저장소 파일이 아니라 참조에서 키 이름을 읽는다.
- [x] 합의된 바인딩 표의 키가 같은 `binding`으로 `REPLACED`다.
- [x] `DB_DSN`류는 `UNREPRESENTABLE`이다.
- [x] EnvPlan에서 계산한 `EnvExceptions`가 기존 결과와 같다.
- [x] 분석 Activity가 값을 읽지 않는다(InMemory 저장소의 `read_values` 호출 0회).

## Test Seam
기존 분석 스캐너 테스트 + ENV-03의 InMemory 저장소.
