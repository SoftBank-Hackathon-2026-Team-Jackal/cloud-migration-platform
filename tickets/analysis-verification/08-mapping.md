# Ticket 08 — 카탈로그·Skill v1과 매핑

## 상태
IN_PROGRESS — 구현 완료(가짜 LLM 기준), 리뷰 대기. Issue 번호는 발행 후 기입

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

- [x] 매핑 행마다 분석 버전 안에서 고유한 `id`가 있고, 다시 매핑해도 같은 사용 방식의 행은 이전 ID를 유지한다.

- [x] Mapper 출력이 스키마 검증을 통과하지 못하면 재시도 후 사람 검토로 넘긴다.
- [x] 카탈로그에 없는 기능은 "검증 안 된 제안"으로만 나온다.
- [x] 모든 매핑 행에 필수 여부 제안과 근거가 있다.
- [x] 처리 표 함수가 등급 × 필수 여부대로 값을 내고 필수 기능 `NONE`은 `BLOCK`이 된다(확정·적용은 08B).
- [x] 첫 사이클의 S3 의존은 `KEEP`으로 표시된다.
- [x] 존재하지 않는 근거를 단 행은 `UNVERIFIED`로 남는다.
- [x] LLM 입력에 마스킹본만 들어간다(입력 캡처 테스트).

## 테스트 전략
- 합의된 Test Seam: 가짜 LLM 클라이언트로 `map_usage` → `check_evidence` 결과. 잡는 것: 스키마·처리 규칙·근거 확인·마스킹. 못 잡는 것: 실제 모델 판단 품질.
- 실행: `uv run pytest tests/analysis/test_mapping.py`, 실제 모델은 데모 앱 1회 실행 기록

## 완료 시 확인할 동작
데모 앱 분석 결과에 매핑 행이 근거·등급·처리·이전/유지·검증 상태와 함께 붙는다.

## 검증 증거
- `uv run pytest tests/agents`: 88 통과(08A 28개 포함). 08 범위는 `test_mapping.py`, `test_catalog.py`, `test_mapper.py`, `test_skills_content.py`다.
  - 처리 표: 등급 × 필수 여부 전 조합, 필수 확정 전에는 미정(None), `PARTIAL` 필수는 대체 방법 증명 시에만 진행, 필수 `NONE`은 `BLOCK`.
  - 행 ID: 같은 (capability, usage)는 이전 ID 유지, 충돌·중복 시에도 고유, 첫 사이클 `object-storage`는 `KEEP`.
  - 근거 확인: 코드 `파일:줄` 존재·범위·경로 이탈 거절, 매니페스트 경로는 스캔한 위치와 대조, 클라우드 쪽 근거는 알려진 리소스 ID가 있을 때만 확인, 카탈로그 밖 기능과 근거 불일치·없음은 `UNVERIFIED`, `SANDBOX_VERIFIED`는 낮추지 않음.
  - 카탈로그: `catalog/*.yaml` 8개 항목(컨테이너 실행, Postgres, 환경 변수, 통신 규칙, Ingress, 볼륨, Pod 신원, 객체 저장)이 모든 대상(AWS·GCP·ON_PREM)의 등급 규칙을 갖고, 참조하는 Skill이 존재하며, 탐지 규칙으로 후보가 골라지고, 잘못된 항목은 거절된다.
  - Mapper: 제안이 미확정·미검증 행으로 변환, LLM 입력에는 마스킹된 증거만 들어가고 카탈로그 후보가 포함, 후보 Skill만 불러올 수 있음, 스키마 실패는 재시도 후 `NEEDS_REVIEW`, 모든 행에 필수 사유와 근거 필수.
  - Skill 4종은 6개 절의 내용과 `examples/` 예시를 갖췄다.
- Helm이 필요한 테스트 파일을 제외한 `pytest` 전체 통과, `ruff check`·`ruff format --check` 통과. 전체 `make check`는 이 PC에 Helm이 없어 실행하지 못했다.
- 구현: `packages/agents/src/cmp_agents/`(`mapping.py`, `catalog.py`, `mapper.py`, `prompts/mapper.md`), `catalog/`, `skills/`.
- 문서 변경: spec·plan 변경 없음(§2.3과 같은 구조).

## 한계와 미검증
- 실제 LLM을 호출하지 않았다. 매핑 품질, 프롬프트의 실제 효과, 모델의 도구 호출 형식은 검증하지 못했다. 실제 연결은 `LLMClient` 어댑터(Anthropic SDK)와 ADR-004(모델·코드 전송 범위·키 관리·비용 한도) 이후 데모 앱 1회 실행으로 기록한다.
- 카탈로그의 등급 규칙과 대상별 차이는 v1 초안이다. GKE·K3s 항목은 일반적인 차이를 적은 것이며 실제 대상에서 검증하지 않았다. 검토가 필요하다.
- 02의 `map_usage`·`check_evidence` Activity는 mock 그대로다. 소스·스캔 결과를 입력으로 연결하는 작업은 이 티켓에서 하지 않았다(분석 Activity 통합은 별도 작업이 필요하다).
- 클라우드 쪽 근거(보안 그룹, IAM, CloudTrail)는 06 결과가 없어 확인하지 못하고 `UNVERIFIED`로 남는다.
- 처리 확정과 `required` 확인은 08B 범위다. 이 티켓은 제안과 표 함수까지다.

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
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
