# Ticket 20 — Claude 클라이언트와 비용 상한 (ADR-004)

## 상태
DONE — PR #114 병합, Issue #113 (가짜 전송과 실제 호출 1회로 검증)

## 목적
Mapper·Tester·Fixer가 실제 Claude(Sonnet 5.5)를 쓰되, 과다 청구를 코드로 막는다.

## 관련 요구사항
AV-007, AV-012, AV-015, AV-020

## 선행 작업
Blocked by: 08A, 08, 12, 14A (모두 구현됨), ADR-004

## 작업 범위
- 포함: Anthropic SDK 어댑터(`AnthropicLLMClient`), 가격표·비용 장부·호출 전 예약과 정산·한도(마이그레이션·일·월)·차단 스위치를 가진 `GovernedLLMClient`, 에이전트 실행의 모델 측 중단 처리(`LLMError`, 예산 초과·거절·오류 시 재시도 없이 이유 코드로 종료), 워커 설정과 클라이언트 공장(기본 꺼짐), 생각 블록 왕복
- 제외: 공유 저장소 기반 장부(워커 복수화 시), 사용량·청구서 대조(운영), 모델 자동 대체(`fallbacks`)

## 수용 기준
- [x] 호출 전에 최악의 비용을 예약하고, 한도를 넘길 수 있으면 보내기 전에 거절한다.
- [x] 마이그레이션당·하루·한 달 한도가 여러 클라이언트·프로세스에서 공유되고 달력이 바뀌면 초기화된다.
- [x] 차단 스위치(파일·환경 변수)가 켜지면 모든 호출이 즉시 거절된다.
- [x] 예산·거절·공급자 오류는 에이전트 실행을 이유 코드와 함께 끝내고 재시도하지 않는다. 장부에는 합계만 남는다.
- [x] 요청에 `temperature`·`thinking`·`tool_choice`를 보내지 않고 `effort`를 보내며, 생각 블록은 그대로 되돌려 보낸다.
- [x] 키는 환경 변수에서 SDK만 읽고, 설정이 꺼져 있거나 키·장부가 없으면 클라이언트가 만들어지지 않는다.
- [x] 실제 키로 짧은 호출 1회가 성공하고 장부에 사용량이 정산된다(비용이 들므로 승인 후 실행).

## 테스트 전략
- 제안 Test Seam: 가짜 응답으로 장부·어댑터·루프, 실제 호출은 짧은 1회. 잡는 것: 한도 계산·동시성·변환·중단 처리. 못 잡는 것: 실제 모델의 품질과 실제 사용량 분포.
- 실행: `uv run pytest tests/agents/test_cost.py tests/agents/test_anthropic_client.py tests/agents/test_llm_factory.py`

## 완료 시 확인할 동작
설정을 켜고 키를 넣으면 Mapper·Fixer가 실제 Claude를 쓰고, 한도를 넘기면 호출 없이 사람 검토로 멈춘다.

## 구현 요약
- `cmp_agents/cost.py`: `PRICES`(sonnet-5-5), `Limits`(기본 $3 / $20 / $100), `CostLedger`(파일 잠금·원자 교체·마이크로 달러 정수), `GovernedLLMClient`.
- `cmp_agents/anthropic_client.py`: 메시지·도구 변환(도구 결과는 다음 user 메시지로 병합), 사용량·캐시 토큰 읽기, 오류 코드화(`LLM_AUTH`, `LLM_BAD_REQUEST`, `LLM_UNAVAILABLE`, `LLM_REFUSED`, `LLM_OUTPUT_TRUNCATED`).
- `cmp_agents/runtime.py`: `LLMError`를 잡아 `AgentRun.fatal` 실패로 반환, 모델 응답의 생각 블록을 다음 턴에 전달. Mapper·Tester·Fixer 루프는 `fatal`이면 중단.
- `cmp_worker/llm_factory.py`·`config.py`: `build_llm_factory`(마이그레이션·역할별 클라이언트, 역할별 `effort`), 설정 `llm_*`. 분석 파이프라인과 Sandbox 루프가 `(migration_id, role)`로 공장을 호출한다.

## 검증 증거
- 실제 호출: `scripts/llm_smoke.py --yes`(2026-11-05, 사용자 실행) 결과 `reply='OK.' in=22 out=5 cache_read=0 booked=$0.000094`. 장부 정산 금액이 가격표 계산(22×$2 + 5×$10 per 1M)과 일치한다.

## 결정
- ADR-004: `docs/decisions/ADR-004.md`.

## 한계와 미검증
- 비용은 가격표 기반 추정이다. 실제 청구와의 대조는 운영 몫이고, 상한 금액은 첫 실행들의 실측 뒤 조정해야 한다.
- 실제 모델의 품질(매핑·테스트·수정)은 미검증이다. 실제 호출은 짧은 1회뿐이라 도구 호출 루프, 생각 블록 왕복, 캐시 읽기는 실제 응답으로 확인하지 않았다.
- 장부는 단일 호스트의 로컬 파일 기준이다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [ADR-004](../../docs/decisions/ADR-004.md)

## 설계 변경 기록
- 2026-11-05: 신설. Sonnet 5.5, 호출 전 예약 방식 비용 상한, fallbacks 미사용으로 결정.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인(실제 호출 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
