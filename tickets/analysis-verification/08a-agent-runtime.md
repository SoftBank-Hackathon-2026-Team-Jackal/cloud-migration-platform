# Ticket 08A — 에이전트 실행 기반과 Skill 형식

## 상태
DONE — PR #68 병합, Issue #67

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
- [x] `FakeLLMClient`로 도구 호출 → 결과 → 제출까지 한 번에 돈다.
- [x] 제출 인자가 스키마에 맞지 않으면 오류가 모델에 돌아가고, 같은 루프 안에서 고쳐 제출하면 통과한다.
- [x] 턴·토큰·시간 예산을 넘으면 실행 기록과 함께 실패한다.
- [x] 허용 목록에 없는 도구 이름을 부르면 거절된다. 셸·네트워크·자격 증명 도구는 등록할 수 없다.
- [x] Skill 로더가 요청한 Skill의 `SKILL.md`만 읽고, 없는 Skill은 명확한 오류를 낸다.
- [x] 실행 기록에 `.env` 값·비밀이 들어가지 않는다(마스킹 입력만).

## 테스트 전략
- 합의된 Test Seam: `run_agent`의 공개 입출력과 실행 기록. 잡는 것: 루프 제어·스키마 강제·예산·허용 목록. 못 잡는 것: 실제 모델의 판단 품질과 응답 형식 특성.
- 실행: `uv run pytest tests/agents/test_runtime.py`, `make check`

## 완료 시 확인할 동작
가짜 LLM으로 짠 시나리오가 Skill을 불러 도구를 쓰고 스키마에 맞는 결과를 제출한다.

## 검증 증거
- `uv run pytest tests/agents/test_runtime.py`: 28 통과. 가짜 LLM으로 도구 호출 → 결과 → 제출, 잘못된 제출 인자가 오류로 돌아가고 같은 루프에서 고쳐 제출하면 통과, 턴·입력 토큰·출력 토큰·벽시계 예산 초과 실패(실행 기록 포함), 허용 목록 밖 도구 거절·미실행, 셸·네트워크·자격 증명 이름의 도구 등록 거부, 도구 인자 검증 오류와 핸들러 예외를 모델에 되돌림, 텍스트만 답한 턴의 재촉, 기록에서 비밀 마스킹, 프롬프트 버전 기록, Skill 로더(요청한 `SKILL.md`만, `examples/` 지연 로딩, 없는 Skill·경로 이탈 이름 거절), `load_skill` 도구와 기록, Skill 4종 골격 존재, 프롬프트 버전(내용 해시)을 확인했다.
- 구현: `packages/agents/src/cmp_agents/`(`llm.py`, `tools.py`, `skills.py`, `runtime.py`, `prompt_store.py`, `prompts/`)와 저장소 루트 `skills/`(`k8s-workload`, `db-postgres`, `network`, `config-env`의 빈 `SKILL.md` 골격). `pyproject.toml`에 `cmp_agents` 패키지와 프롬프트 데이터를 등록했다.
- Helm이 필요한 테스트 파일을 제외한 `pytest` 전체 통과, `ruff check`·`ruff format --check` 통과. 전체 `make check`는 이 PC에 Helm이 없어 실행하지 못했다.
- 문서 변경: plan §2.11과 같은 구조라 spec·plan 변경 없음.

## 한계와 미검증
- 실제 LLM 공급자는 연결하지 않았다(D-01). `FakeLLMClient`가 정한 응답만 재생하므로 실제 모델의 도구 호출 형식과 판단 품질은 검증하지 못했다.
- 도구 허용 목록은 이름 기반 금지 패턴(`shell`, `http`, `credential` 등)이라 우회하는 이름은 막지 못한다. 실제 도구 구현은 08·12·14에서 목록에 맞춰 등록하고 검토한다.
- 기록의 마스킹은 03의 `mask_text` 규칙 기준이다. 규칙이 못 잡는 비밀은 기록에 남을 수 있다.
- Skill 4종은 제목만 있는 골격이다. 내용은 08·12·14에서 채운다. `scripts/`와 카탈로그(`catalog/`) 연결은 이 티켓 범위 밖이다.
- 02의 Activity 연결은 08 이후에 한다.

## 결정 대기
- D-01 LLM 공급자·모델(실제 연결 시)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 에이전트 실행 방식과 Skill 형식이 문서에 없던 점을 보완하려고 신설(plan §2.11).

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
