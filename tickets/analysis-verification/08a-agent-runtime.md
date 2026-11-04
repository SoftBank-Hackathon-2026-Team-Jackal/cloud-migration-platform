# Ticket 08A — 에이전트 실행 기반과 Skill 형식

## 상태
DRAFT — 사용자 검토 전

## 목적
Mapper·Tester·Fixer가 공통으로 쓰는 LLM 도구 호출 루프, 도구 허용 목록, Skill 로더를 Temporal과 무관한 패키지로 만든다.

## 관련 요구사항
AV-007, AV-020

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: `cmp_agents.llm`(`LLMClient` 인터페이스, `FakeLLMClient`), `runtime.run_agent`(도구 루프, 제출 도구 스키마 강제, 턴·토큰·시간 예산), `tools`(허용 목록 등록), `skills`(카탈로그가 가리키는 `SKILL.md`만 로딩, `examples/` 지연 로딩), 실행 기록(프롬프트 버전·Skill·도구 호출 요약·결과), `skills/<name>/` 디렉터리 형식과 빈 Skill 4종 골격, `prompts/` 디렉터리
- 제외: 실제 LLM 공급자 연결(D-01), 역할별 판단 로직(08·12·14)

## 수용 기준
- [ ] `FakeLLMClient`로 도구 호출 → 결과 → 제출까지 한 번에 돈다.
- [ ] 제출 인자가 스키마에 맞지 않으면 오류가 모델에 돌아가고, 같은 루프 안에서 고쳐 제출하면 통과한다.
- [ ] 턴·토큰·시간 예산을 넘으면 실행 기록과 함께 실패한다.
- [ ] 허용 목록에 없는 도구 이름을 부르면 거절된다. 셸·네트워크·자격 증명 도구는 등록할 수 없다.
- [ ] Skill 로더가 요청한 Skill의 `SKILL.md`만 읽고, 없는 Skill은 명확한 오류를 낸다.
- [ ] 실행 기록에 `.env` 값·비밀이 들어가지 않는다(마스킹 입력만).

## 테스트 전략
- 제안 Test Seam: `run_agent`의 공개 입출력과 실행 기록. 잡는 것: 루프 제어·스키마 강제·예산·허용 목록. 못 잡는 것: 실제 모델의 판단 품질과 응답 형식 특성.
- 실행: `uv run pytest tests/agents/test_runtime.py`, `make check`

## 완료 시 확인할 동작
가짜 LLM으로 짠 시나리오가 Skill을 불러 도구를 쓰고 스키마에 맞는 결과를 제출한다.

## 결정 대기
- D-01 LLM 공급자·모델(실제 연결 시)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 에이전트 실행 방식과 Skill 형식이 문서에 없던 점을 보완하려고 신설(plan §2.11).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
