# Ticket 23 — 비공개 소스 접근 (토큰 참조와 교체 가능한 해석기)

## 상태
DONE — PR #132 병합, Issue #131

## 목적
GitHub 로그인이 생기면 플랫폼이 사용자 토큰을 보관한다. 그 전에도 비공개 저장소를 읽을 수 있게 하되, 로그인이 붙을 때 **분석·복제 코드를 바꾸지 않고 토큰 출처만 교체**할 수 있게 한다.

## 관련 요구사항
AV-001, AV-002, AV-020

## 선행 작업
Blocked by: 03, 19, 21 (모두 병합됨). 로그인·토큰 저장은 이 티켓의 범위가 아니다(민서님, 회의에서 경계 확정 예정).

## 작업 범위
- 포함: `AnalysisInput.source_access_ref`(`VersionRef`, 선택), 토큰을 푸는 인터페이스 `SourceTokenResolver`(읽기 전용, 용도 `READ`/`WRITE`), 기본 해석기(`none`)와 개발용 해석기(`env`: 워커 환경 변수 `CMP_GITHUB_TOKEN` 하나), 토큰이 가는 주소 검증 `github_repository`, 분석 소스 확보 단계 연결, 거절은 재시도 없이 코드로 실패, 설정 `SOURCE_TOKEN_RESOLVER`, 사전 점검·시작 스크립트(`--source-access dev-env`)와 실행 안내 갱신
- 제외: GitHub OAuth 로그인, 토큰 저장소·DB·세션(민서님), 저장소 목록·커밋 선택 API, PR 생성(`DraftPrPublisher`의 GitHub 구현, 같은 해석기를 `WRITE`로 쓴다)

## 수용 기준
- [x] 참조가 있을 때만 토큰이 복제에 쓰이고, 참조가 없으면 지금과 똑같이 동작한다.
- [x] 토큰은 `https://github.com/<owner>/<repo>` 형태의 주소에만 보낸다. 다른 호스트, 사용자 정보, 포트, 질의, 경로 속임수는 토큰을 읽기 전에 거절한다.
- [x] 입력은 참조(이름)만 담고 토큰 문자열은 담을 수 없다. 개발용 해석기는 고정된 하나의 환경 변수만 읽고 입력이 다른 변수를 고르지 못한다.
- [x] 토큰은 명령줄 인자, Workflow 입력·이력, 결과, 오류 메시지, 로그 출력에 나타나지 않는다(자격 증명 객체는 출력해도 `***`).
- [x] 토큰 저장소가 없거나 토큰이 없으면 토큰 없이 복제를 시도하지 않고 재시도 없이 이유 코드로 실패한다.
- [x] 같은 인터페이스가 읽기와 나중의 쓰기(PR)에 모두 쓰인다.

## 테스트 전략
- 제안 Test Seam: 해석기(순수), 주소 검증(순수), 분석 단계(복제 함수 대체), 실제 복제 함수(`subprocess` 대체로 환경 변수 전달 확인). 잡는 것: 토큰 노출 경로, 허용 주소, 기본 동작 유지. 못 잡는 것: 실제 GitHub와의 통신, 실제 비공개 저장소(수동 확인).
- 실행: `uv run pytest tests/analysis/test_source_access.py tests/sandbox/test_preflight.py`

## 완료 시 확인할 동작
`SOURCE_TOKEN_RESOLVER=env`와 `CMP_GITHUB_TOKEN`을 워커에 주고 `--source-access dev-env`로 시작하면 비공개 GitHub 저장소가 분석된다. 로그인이 붙으면 설정값과 해석기 구현만 바뀐다.

## 구현 요약
- `cmp_worker/source_access.py`: `SourceTokenResolver`(계약: 읽기 전용, `SOURCE_ACCESS_NOT_CONFIGURED`/`SOURCE_TOKEN_UNAVAILABLE`/`SOURCE_ACCESS_DENIED`), `SourceCredential`(출력 시 가림), `NoSourceTokenResolver`, `EnvSourceTokenResolver`, `github_repository`, `build_source_resolver`.
- `AnalysisPipeline.fetch_source`: 참조가 있으면 주소 검증 → 해석 → 기존 복제 함수에 토큰 전달(환경 변수로 git에 전달하고 출력에서 가림). `AnalysisActivities._run`은 `SourceAccessError`를 재시도 없는 `ApplicationError`로 바꾼다.
- 부모·분석 Workflow 코드는 바꾸지 않았다(분석 입력이 그대로 흐르므로 새 필드는 안에서 전달된다).

## 로그인이 붙을 때 바뀌는 곳 (예상)
1. 민서님이 `SourceTokenResolver`를 구현한다(참조 → DB의 토큰, 폐기·만료 시 `SOURCE_TOKEN_UNAVAILABLE`).
2. `build_source_resolver`에 이름 하나를 더한다(예: `platform`).
3. 입력의 `source_access_ref`를 API가 사용자 연결 참조로 채운다. 분석·복제 코드는 그대로다.

## 한계와 미검증
- 실제 GitHub 비공개 저장소로는 아직 확인하지 않았다(개인 토큰을 준 수동 실행 필요, 실행 안내 참조).
- 개발용 해석기는 토큰 하나만 지원한다(사용자별 구분 없음). 여러 사용자는 로그인 구현의 몫이다.
- 토큰이 폐기되었는지 미리 확인하지 않는다(복제 실패로 드러남).

## 관련 문서
- [실행 안내](../../specs/analysis-verification/runbook.md) · [요구사항](../../specs/analysis-verification/spec.md) · [티켓 18](18-public-github-source.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)

## 설계 변경 기록
- 2026-11-05: 신설. 티켓 18이 전제한 "플랫폼은 GitHub 토큰을 받지 않는다"를 "토큰은 플랫폼이 보관하고 분석은 참조로만 쓴다"로 바꾼다(승철님 결정, 보관 구현은 민서님과 경계 협의).

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
