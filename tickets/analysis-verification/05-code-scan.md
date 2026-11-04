# Ticket 05 — 코드 스캔과 `.env` 대조

## 상태
IN_PROGRESS — 구현 완료(Go 어댑터 포함), 리뷰 대기, Issue #57

## 목적
앱 코드에서 클라우드 SDK 호출, 환경변수 읽기, DB 드라이버, 외부 호출, 확장 생성을 근거와 함께 뽑고 `.env`와 대조한다.

## 관련 요구사항
AV-003

## 선행 작업
Blocked by: 03, D-07(언어별 어댑터만)

## 작업 범위
- 포함(언어 무관, 먼저 진행): 언어 공통 `CodeScanner` 인터페이스와 결과 모델(호출 지점: 종류·대상·파일·줄), 언어 감지와 미지원 파일 "스캔 불가" 표시, SQL 마이그레이션의 `CREATE EXTENSION` 탐지, `.env` 이름과 코드가 읽는 변수 대조
- 포함(D-07 후): 첫 지원 언어 어댑터(클라우드 SDK 호출, 환경변수 읽기, DB 드라이버, 외부 호스트 호출)
- 제외: 두 번째 언어부터, 실행 커버리지, 코드 색인(17)

## 수용 기준
- [x] (D-07 후) AWS SDK 호출이 서비스·동작·파일·줄로 나온다.
- [x] (D-07 후) 문자열 상수로 된 외부 호스트 호출이 `network_intent`의 `external_host` 후보로 나온다.
- [x] SQL 마이그레이션이 확장을 스스로 만드는지 나온다.
- [x] 대조 로직이 어댑터 결과 형식만 받아 동작하고, `.env`에만 있는 변수와 코드가 읽지만 `.env`에 없는 변수가 구분된다.
- [x] 지원하지 않는 언어 파일은 "스캔 불가"로 표시하고 무시하지 않는다.

## 테스트 전략
- 합의된 Test Seam: `scan_code(workspace, env_names)` 결과. 잡는 것: 정적 호출 지점. 못 잡는 것: 동적 호출(문자열 조합 서비스명 등, 13의 출구 기록이 담당).
- 실행: 언어 무관 부분은 가짜 어댑터로, 어댑터는 해당 언어 fixture 코드로 `uv run pytest tests/analysis/test_code_scan.py`

## 완료 시 확인할 동작
fixture 코드에서 호출 지점·외부 호스트·확장 생성 여부와 `.env` 대조 결과가 나온다.

## 검증 증거 (언어 무관 부분)
- `uv run pytest tests/analysis/test_code_scan.py`: 11 통과. 가짜 어댑터로 호출 지점의 상대 경로·줄, 어댑터 없는 소스 파일의 "스캔 불가" 표시(비소스 파일은 제외), 어댑터 오류 시 해당 파일만 스캔 불가 처리, `.sql`과 소스 문자열의 `CREATE EXTENSION` 탐지(주석 제외, 이름·파일·줄), 확장 없음의 명시적 false, `.env` 대조 분리와 부분 스캔 표시, `.env`·`.git`·의존성 디렉터리 제외, 큰 파일·비텍스트 파일 건너뜀 보고, 없는 작업 공간 거절을 확인했다.
- 구현: `packages/scanners/src/cmp_scanners/code.py`(`scan_code`, `LanguageAdapter`, `CallSite`). 결과에는 이름·파일·줄만 담고 소스 본문은 넣지 않는다.
- Go 어댑터(`packages/scanners/src/cmp_scanners/go_adapter.py`): `uv run pytest tests/analysis/test_code_scan_go.py` 7 통과. fixture(`tests/fixtures/analysis/go_app/`)로 `os.Getenv`·`os.LookupEnv`·`getenv("이름", 기본값)` 헬퍼의 환경 변수 읽기, AWS SDK v2 서비스 import와 `<서비스>.<동작>` 호출(별칭 import 포함), `http.Get`·`NewRequest`의 문자열 URL에서 외부 호스트(localhost 제외, 쿼리·자격 증명 제외), pgx·lib/pq 등 DB 드라이버 import, 주석 처리된 호출 무시를 확인했다.
- 실제 데모 앱 확인: `demo-app`의 `main.go`·`store.go`·`hub.go`를 임시 폴더에 받아 스캔했다(저장소에는 복사하지 않음). 환경 변수 5개(`ENV_NAME`, `ADDR`, `DATABASE_URL`, `POLL_INTERVAL_MS`, `DRAIN_DELAY`)와 pgx 드라이버 2건이 나왔고 AWS 호출·외부 호스트·확장 생성은 없었다. README의 설정 표와 일치한다.
- 남은 것: 02의 `scan_code` Activity는 mock 그대로이며 연결은 07에서 한다.
- Go 어댑터 한계: Go 파서가 아니라 줄 단위 규칙이다. 변수에 담긴 URL, 문자열로 조합한 서비스명, 여러 줄에 걸친 호출은 놓친다. 환경 변수 헬퍼는 이름 목록(`getenv`, `getEnv`, `envOr` 등)에 있는 것만 인식한다. AWS 호출은 `<서비스>.<동작>Input` 타입 사용 기준이라 입력 타입 없이 부르는 호출은 import 수준으로만 보인다.
- 한계: 대조는 어댑터가 읽은 변수만 기준이라, 어댑터가 없거나 오류가 난 파일이 있으면 `partial=true`로 표시하고 "안 쓰는 변수" 결과를 신뢰하지 않는다. 확장 탐지는 `CREATE EXTENSION` 문자열을 줄 단위로 찾으므로 여러 줄에 걸친 문장이나 동적으로 조합한 SQL은 놓친다.

## 결정
- 첫 사이클 지원 언어(D-07): Go로 결정(2026-11-05, 승철).

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: egress 후보와 확장 생성 탐지 추가.
- 2026-11-04(v2 동기화): main의 v2 구현(#22, #24, B-01 변경) 기준으로 수정.
- 2026-11-04(INFRA·D-07 반영): 첫 사이클 지원 언어 미정(D-07)을 기준으로, 언어 무관 부분과 언어별 어댑터를 나누고 어댑터만 D-07에 묶음.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
