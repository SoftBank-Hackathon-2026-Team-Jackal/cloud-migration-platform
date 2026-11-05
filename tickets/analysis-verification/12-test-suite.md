# Ticket 12 — 테스트 묶음·Tester·`run_suite`

## 상태
DONE — PR #101 병합, Issue #97 (실제 k3d `cmp-sandbox`에서 검증)

## 목적
Sandbox와 Production에서 같은 정의로 돌릴 앱 기능 테스트 묶음과 실행기를 만들고, AI 생성 테스트는 네 조건으로 검증해 고정한다.

## 관련 요구사항
AV-012, AV-020

## 선행 작업
Blocked by: 07, 08A, 11

## 작업 범위
- 포함: HTTP 시나리오 형식, 테스트별 `READ_ONLY`/`WRITES_DATA`·외부 부수효과 표시, 표준 테스트(헬스, DB 쓰기→읽기 일치, 재시작 후 유지), `generate_tests`(Tester, 별도 세션), `validate_tests` 네 조건, SHA-256 묶음 저장(D-02: 해시 이름 로컬 저장소, S3는 후속), `packages/testsuite`의 `run_suite(suite_ref, base_url, auth_ref, include)`
- 제외: 실행 커버리지 계측

## 수용 기준
- [x] 표준 테스트가 Sandbox에서 통과한다.
- [x] 상태 코드만 보는 AI 테스트는 2번 조건에서 거절된다.
- [x] DB 연결을 끊었을 때 실패하지 않는 테스트는 3번 조건에서 거절된다.
- [x] 고정 후 묶음 해시가 바뀌면 `run_suite`가 실행을 거부한다.
- [x] 외부 부수효과가 있는 테스트는 기본 실행에서 빠지고, `include=READ_ONLY`로 읽기 테스트만 돌릴 수 있다.
- [x] Tester 세션 입력에 Fixer 수정 내용이 없다.

## 테스트 전략
- 제안 Test Seam: 가짜 LLM 응답 + k3d Sandbox에서 `validate_tests`·`run_suite` 결과. 잡는 것: 인정 조건·고정·필터. 못 잡는 것: 테스트가 다루지 못한 기능.
- 실행: `uv run pytest tests/sandbox/test_suite.py`, k3d 통합 테스트

## 완료 시 확인할 동작
데모 앱 테스트 묶음 참조(SHA-256)가 나오고 같은 묶음을 다른 `base_url`로 `run_suite`할 수 있다.

## 구현 요약
- `packages/testsuite/cmp_testsuite`: `SuiteCase`(HTTP 시나리오 + `READ_ONLY`/`WRITES_DATA` + `external_side_effects`), 결정적 tar.gz 묶음(`dump_bundle`), `run_suite(suite_ref, base_url, auth_ref, include, store=...)`.
- `run_suite`는 저장소에서 읽은 바이트의 SHA-256이 `suite_ref`와 다르면 `SUITE_HASH_MISMATCH`로 거부한다. 외부 부수효과 케이스는 `include_external=True`가 아니면 건너뛰고, `include="READ_ONLY"`는 읽기 케이스만 돌린다. 인증은 `auth_ref`를 호출부가 주입한 해석기로 헤더로 바꾼다. 결과에는 응답 내용 없이 오류 코드만 남는다.
- 표준 테스트(`standard_cases`): 헬스, 앱 API로 쓰기→읽기 일치, DB 재시작 후 유지(`RESTART_DATABASE` 액션 단계).
- `validate_tests`: 조건 2(값 assertion 정적 검사) → 조건 1(원본 실행기) → 대상 통과 → 조건 3(의존성 차단 후 실패해야 함). 원본 실행기나 차단 수단이 없으면 거절한다(`ORIGIN_UNAVAILABLE`, `CUT_UNAVAILABLE`). 조건 4는 `pin_suite`가 통과한 케이스로 묶음 바이트를 만들고 호출부가 저장해 SHA-256을 고정한다.
- Tester(`cmp_agents.tester.generate_tests`): 입력(`SuiteRequest`)에 사용 요약·API 목록·기존 테스트 id만 있고 Fixer 수정 내용을 담을 필드가 없다. 제출은 `source=AI`만 허용하고 Pydantic으로 검증한다.
- Sandbox 지원(`SandboxService`/`SandboxCluster`): `restart_database`, `database_cut`(StatefulSet scale 0 → 복구), `service_path`, `kubectl proxy` 컨텍스트로 클러스터 밖에서 앱에 접근.

## 결정 (이전 "결정 대기")
- D-06 일부: 묶음은 D-02의 `ArtifactStore`에 `ref_id=suite`로 저장한다. S3 구현은 같은 인터페이스 뒤에 후속으로 붙인다.
- D-09는 계속 대기: 1번 조건의 원본 쪽 테스트 자원은 `origin` 실행기 인터페이스로만 두고 가짜로 검증했다.
- 의존성 차단은 DB scale 0만 구현했다. 외부 호스트 차단(NetworkPolicy·프록시)은 13에서 같은 `cut_dependency` 인터페이스에 추가한다.

## 검증 증거
- `uv run pytest tests/testsuite tests/agents/test_tester.py tests/sandbox`.
- 실제 k3d: `uv run pytest tests/integration/sandbox -m k3d -k pinned_suite` 통과. `kubectl proxy`로 앱에 접근해 표준 헬스와 DB 재시작 후 헬스 통과, 묶음 바이트 변경 시 거부, DB를 쓰지 않는 podinfo 테스트는 `PASSES_WITHOUT_DEPENDENCY`로 거절, 차단 뒤 DB 복구 확인.

## 한계와 미검증
- 공개 앱 podinfo는 DB를 쓰지 않아 "DB 쓰기→읽기"와 "재시작 후 유지" 표준 테스트는 단위 테스트(가짜 서버)로만 검증했다. 데모 앱 이미지로의 실행은 남음.
- 조건 1의 실제 원본 실행(D-09), 실제 Claude로 만든 테스트의 품질은 미검증.
- Workflow·Activity 연결과 S3 저장은 14·후속.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: test_suite_ref 형식(S3 SHA-256)·run_suite 제공·테스트 표시 반영.
- 2026-11-04(목적 점검): Tester가 08A 실행 기반을 쓰도록 선행 작업 추가.
- 2026-11-05: 구현. 저장은 D-02 로컬 저장소로, 조건 3은 DB scale 0으로 먼저 구현하고 외부 호스트 차단은 13으로 넘김.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
