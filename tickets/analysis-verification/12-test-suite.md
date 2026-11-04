# Ticket 12 — 테스트 묶음·Tester·`run_suite`

## 상태
DRAFT — 사용자 검토 전

## 목적
Sandbox와 Production에서 같은 정의로 돌릴 앱 기능 테스트 묶음과 실행기를 만들고, AI 생성 테스트는 네 조건으로 검증해 고정한다.

## 관련 요구사항
AV-012, AV-020

## 선행 작업
Blocked by: 07, 08A, 11

## 작업 범위
- 포함: HTTP 시나리오 형식, 테스트별 `READ_ONLY`/`WRITES_DATA`·외부 부수효과 표시, 표준 테스트(헬스, DB 쓰기→읽기 일치, 재시작 후 유지), `generate_tests`(Tester, 별도 세션), `validate_tests` 네 조건, SHA-256 묶음 S3 저장, `packages/testsuite`의 `run_suite(suite_ref, base_url, auth_ref, include)`
- 제외: 실행 커버리지 계측

## 수용 기준
- [ ] 표준 테스트가 Sandbox에서 통과한다.
- [ ] 상태 코드만 보는 AI 테스트는 2번 조건에서 거절된다.
- [ ] DB 연결을 끊었을 때 실패하지 않는 테스트는 3번 조건에서 거절된다.
- [ ] 고정 후 묶음 해시가 바뀌면 `run_suite`가 실행을 거부한다.
- [ ] 외부 부수효과가 있는 테스트는 기본 실행에서 빠지고, `include=READ_ONLY`로 읽기 테스트만 돌릴 수 있다.
- [ ] Tester 세션 입력에 Fixer 수정 내용이 없다.

## 테스트 전략
- 제안 Test Seam: 가짜 LLM 응답 + k3d Sandbox에서 `validate_tests`·`run_suite` 결과. 잡는 것: 인정 조건·고정·필터. 못 잡는 것: 테스트가 다루지 못한 기능.
- 실행: `uv run pytest tests/sandbox/test_suite.py`, k3d 통합 테스트

## 완료 시 확인할 동작
데모 앱 테스트 묶음 참조(SHA-256)가 나오고 같은 묶음을 다른 `base_url`로 `run_suite`할 수 있다.

## 결정 대기
- 1번 조건의 원본 쪽 테스트 자원(D-09)
- 테스트 묶음 버킷(D-06)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: test_suite_ref 형식(S3 SHA-256)·run_suite 제공·테스트 표시 반영.
- 2026-11-04(목적 점검): Tester가 08A 실행 기반을 쓰도록 선행 작업 추가.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
