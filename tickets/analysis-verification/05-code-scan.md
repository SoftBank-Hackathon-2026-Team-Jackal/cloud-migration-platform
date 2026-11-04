# Ticket 05 — 코드 스캔과 `.env` 대조

## 상태
DRAFT — 사용자 검토 전

## 목적
앱 코드에서 클라우드 SDK 호출, 환경변수 읽기, DB 드라이버, 외부 호출, 확장 생성을 근거와 함께 뽑고 `.env`와 대조한다.

## 관련 요구사항
AV-003

## 선행 작업
Blocked by: 03

## 작업 범위
- 포함: Python `ast` 스캐너(boto3 client/resource·메서드, `os.environ`/`getenv`, psycopg/SQLAlchemy, requests/httpx 외부 호스트), 마이그레이션의 `CREATE EXTENSION` 탐지, `.env` 이름 대조
- 제외: 다른 언어(tree-sitter), 실행 커버리지, 코드 색인(17)

## 수용 기준
- [ ] boto3 호출이 서비스·동작·파일·줄로 나온다.
- [ ] 문자열 상수로 된 외부 호스트 호출이 egress 후보로 나온다.
- [ ] 마이그레이션이 확장을 스스로 만드는지 나온다.
- [ ] `.env`에만 있는 변수와 코드가 읽지만 `.env`에 없는 변수가 구분된다.
- [ ] 지원하지 않는 언어 파일은 "스캔 불가"로 표시하고 무시하지 않는다.

## 테스트 전략
- 제안 Test Seam: `scan_code(workspace, env_names)` 결과. 잡는 것: 정적 호출 지점. 못 잡는 것: 동적 호출(문자열 조합 서비스명 등, 13의 출구 기록이 담당).
- 실행: fixture 앱 코드, `uv run pytest tests/analysis/test_code_scan.py`

## 완료 시 확인할 동작
fixture 코드에서 호출 지점·외부 호스트·확장 생성 여부와 `.env` 대조 결과가 나온다.

## 결정 대기
- 데모 앱 언어(Python이 아니면 이 티켓 BLOCKED, D-07)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: egress 후보와 확장 생성 탐지 추가.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
