# Ticket 02 — 분석 Activity 골격

## 상태
DRAFT — 사용자 검토 전

## 목적
스캐폴드 `analyze_project`를 분석 Activity 묶음으로 바꾸고 병렬 스캔 흐름을 fixture 반환으로 연결한다.

## 관련 요구사항
AV-001, AV-021

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: 분석 Activity 골격(fixture 반환), 스캔 4종 병렬 실행, `build_requirements`·`precheck`·`map_usage`·`check_evidence` 자리, Workflow 연결
- 제외: Sandbox 자식 Workflow(02B), 실제 스캔·LLM

## 수용 기준
- [ ] 스캔 4종이 병렬로 실행되는 것이 이력에서 보인다.
- [ ] 분석 결과로 01의 `AnalysisReport`·`Requirements` fixture가 반환된다.
- [ ] Workflow 코드에 외부 I/O·시각·난수가 없다(Temporal 샌드박스 통과).
- [ ] 기존 승인 대기 스캐폴드 흐름이 깨지지 않는다.

## 테스트 전략
- 제안 Test Seam: Temporal 테스트 환경의 Workflow 결과·이력. 잡는 것: 순서·병렬·계약 형태. 못 잡는 것: 실제 스캔 시간.
- 실행: `uv run pytest tests/analysis/test_workflow.py`, `make check`, `make smoke-temporal`

## 완료 시 확인할 동작
로컬 Temporal에서 분석 단계가 fixture로 끝까지 돈다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리뷰 피드백으로 기존 02를 분석 골격(02)과 Sandbox 자식 Workflow(02B)로 분할.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
