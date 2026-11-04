# Ticket 08 — 카탈로그·Skill v1과 매핑

## 상태
DRAFT — 사용자 검토 전

## 목적
카탈로그와 Skill로 Mapper가 등급·잃는 것·필수 여부·이전/유지를 제안하고, 근거 확인은 기계적으로 한다. 처리는 사용자 확정(08B) 뒤에 계산한다.

## 관련 요구사항
AV-007, AV-008, AV-020

## 선행 작업
Blocked by: 07, 08A, D-01(실제 LLM 연결 시)

## 작업 범위
- 포함: `catalog/` 첫 버전(컨테이너 실행·Postgres·환경변수·네트워크·Ingress·볼륨·Pod 신원·객체 저장, 각 항목의 Skill 이름), Skill 4종의 `SKILL.md`·`examples/` 내용, `cmp_agents.mapper`(08A 실행 기반 사용)와 `prompts/mapper.md`, `map_usage`·`check_evidence` Activity, 필수 여부 제안(`required_proposed`·근거), 처리 표 함수, `MIGRATE`/`KEEP` 구분
- 제외: Tester·Fixer, S3 코드 수정

## 수용 기준
- [ ] 매핑 행마다 분석 버전 안에서 고유한 `id`가 있고, 다시 매핑해도 같은 사용 방식의 행은 이전 ID를 유지한다.
- [ ] Mapper 출력이 스키마 검증을 통과하지 못하면 재시도 후 사람 검토로 넘긴다.
- [ ] 카탈로그에 없는 기능은 "검증 안 된 제안"으로만 나온다.
- [ ] 모든 매핑 행에 필수 여부 제안과 근거가 있다.
- [ ] 처리 표 함수가 등급 × 필수 여부대로 값을 내고 필수 기능 `NONE`은 `BLOCK`이 된다(확정·적용은 08B).
- [ ] 첫 사이클의 S3 의존은 `KEEP`으로 표시된다.
- [ ] 존재하지 않는 근거를 단 행은 `UNVERIFIED`로 남는다.
- [ ] LLM 입력에 마스킹본만 들어간다(입력 캡처 테스트).

## 테스트 전략
- 제안 Test Seam: 가짜 LLM 클라이언트로 `map_usage` → `check_evidence` 결과. 잡는 것: 스키마·처리 규칙·근거 확인·마스킹. 못 잡는 것: 실제 모델 판단 품질.
- 실행: `uv run pytest tests/analysis/test_mapping.py`, 실제 모델은 데모 앱 1회 실행 기록

## 완료 시 확인할 동작
데모 앱 분석 결과에 매핑 행이 근거·등급·처리·이전/유지·검증 상태와 함께 붙는다.

## 결정 대기
- D-01 LLM 공급자·모델·전송 범위

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 출구 규칙에 쓰는 MIGRATE/KEEP 구분과 대문자 Enum 반영.
- 2026-11-04(목적 점검): 실행 기반을 08A로 분리, 필수 여부 제안 추가. 처리 확정은 08B 게이트 뒤로.
- 2026-11-04(01 리뷰 반영): 매핑 행 ID 부여·유지를 수용 기준에 추가.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
