# Ticket 14A — 실패 분류와 Fixer

## 상태
IN_PROGRESS — 구현 완료, 리뷰 대기, Issue 번호 미기록

## 목적
실패한 시도를 결정적으로 분류하고, 고칠 수 있는 실패에만 Fixer가 수정안을 내게 한다. 티켓 14를 14A·14B·14C로 나눈 첫 조각이다.

## 관련 요구사항
AV-015, AV-017

## 선행 작업
Blocked by: 08, 10, 11B, 13 (모두 구현됨)

## 작업 범위
- 포함: `classify_failure`(INFRA → RENDER → BUILD → SPEC(차단된 호출) → CODE → SPEC 순서), 렌더 결과와 요구사항의 필드 단위 비교(`find_render_mismatches`), Fixer(`fix_mapping`: 요구사항 필드·intent 수정, `fix_code`: 파일 수정), 수정안을 기계적으로 적용하는 함수, 테스트·환경 파일 수정 거절, INFRA·RENDER에서 모델 호출 금지
- 제외: 루프 Workflow·Activity 연결·종료 상태(14B), 최종 발행·재승인·패치 산출물(14C), 실제 Claude 연결

## 수용 기준
- [x] 포트 오류(헬스 실패, 앱 스택 없음)는 SPEC으로 분류된다.
- [x] intent 누락으로 출구가 차단되면 SPEC으로 분류되고 이유에 호스트가 나온다.
- [x] 앱 스택이 있는 테스트 실패는 CODE, 빌드 실패는 BUILD로 분류된다.
- [x] 렌더 결과가 요구사항과 다르면 RENDER로 분류되고 Fixer가 호출되지 않는다.
- [x] 클러스터 접근 불가·레지스트리 접근 실패·앱 원인 없는 시간 초과는 INFRA로 분류되고 Fixer가 호출되지 않는다.
- [x] Fixer가 테스트 파일·테스트 디렉터리·`.env`·작업 공간 밖 경로를 바꾸려 하면 거절되고 에이전트가 고쳐 다시 낼 수 있다.
- [x] 같은 입력이면 같은 분류가 나온다.

## 테스트 전략
- 제안 Test Seam: `classify_failure`(순수 함수), `propose_fix`(가짜 LLM), `apply_*_fix`. 잡는 것: 분류 순서·거절 규칙·적용 결과. 못 잡는 것: 실제 모델의 수정 품질, 실제 클러스터에서의 신호 수집.
- 실행: `uv run pytest tests/sandbox/test_classifier.py tests/agents/test_fixer.py`

## 완료 시 확인할 동작
주입한 실패 신호가 정해진 종류로 분류되고, 고칠 수 있는 종류에만 수정안이 나오며 수정안은 테스트를 건드리지 못한다.

## 구현 요약
- `cmp_worker/failure_classifier.py`: `AttemptSignals`(SandboxError 코드, Pod 이벤트, 시간 초과, 렌더 불일치, 빌드 분류, `Verdict`, 앱 스택 여부) → `Classification`(종류, 이유, `fixer_allowed`).
- `cmp_agents/fixer.py`: `RequirementsFix`(port·health_path·replicas·cpu·memory·storage 수정과 intent 추가·삭제, 자원 추가·삭제 불가), `CodeFix`(파일 전체 교체, 보호 경로 검사), `apply_requirements_fix`(결과를 `RequirementsV2`로 다시 검증), `apply_code_fix`, `propose_fix`(INFRA·RENDER는 모델 호출 전 거절, 로그·요구사항 마스킹, 테스트는 읽기 전용 조회만).

## 한계와 미검증
- 분류 신호(Pod 이벤트, 앱 스택 여부, 시간 초과)를 클러스터와 로그에서 모으는 일은 14B의 Activity가 한다. 이 티켓은 신호가 주어졌다고 보고 분류한다.
- 실제 모델이 만든 수정안의 품질은 미검증이다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-05: 티켓 14를 14A·14B·14C로 분할(14A: 분류·Fixer, 14B: 루프·종료 상태, 14C: 발행·재승인·패치 산출물).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
