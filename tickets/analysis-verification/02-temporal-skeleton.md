# Ticket 02 — 분석 Activity 골격

## 상태
DRAFT — 사용자 검토 전

## 목적
`AnalysisWorkflow` 자식에 분석 Activity 묶음과 병렬 스캔 흐름을 fixture 반환으로 연결한다.

## 관련 요구사항
AV-001, AV-021

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: `workflows/analysis.py`의 `AnalysisWorkflow`와 분석 Activity 골격(fixture 반환), 스캔 4종 병렬 실행, `build_requirements`·`precheck`·`map_usage`·`check_evidence` 자리, 부모의 분석 자식 호출·결과·실패·취소 처리, Workflow Worker의 자식 등록과 `workers/analysis.py`의 Activity 등록. 자식 ID는 `analysis-<migration_id>`로 고정한다.
- 제외: Sandbox 자식 Workflow(02B), 실제 스캔·LLM

## 수용 기준
- [ ] 스캔 4종이 병렬로 실행되는 것이 이력에서 보인다.
- [ ] 분석 결과로 01의 `AnalysisReport`·`RequirementsV2` fixture가 반환된다.
- [ ] Workflow 코드에 외부 I/O·시각·난수가 없다(Temporal 샌드박스 통과).
- [ ] 기존 승인 대기 스캐폴드 흐름이 깨지지 않는다.
- [ ] 부모가 분석 자식의 완료·실패·취소를 처리한다. 공유 부모 파일의 변경 범위와 replay 결과를 PR 설명에 기록해 민서님께 공유한다.
- [ ] 기존 진행 중 Workflow 이력과의 replay 호환성을 검토·검증하고 필요하면 안전한 버전 분기를 적용한다.
- [ ] 기존 Temporal 기본 Data Converter를 유지한다. Activity 경계에서 Pydantic 모델을 `model_dump(mode="json")`·`model_validate`로 처리하고, Workflow·Activity 경계의 fixture 왕복과 잘못된 입력 거절을 실제 Converter로 검증한다. 이력에는 참조·작은 메타데이터만 담는다.

## 테스트 전략
- 제안 Test Seam: Temporal 테스트 환경의 Workflow 결과·이력과 실제 기본 Data Converter, 변경 전 Workflow 이력 replay. 잡는 것: 순서·병렬·계약 직렬화, 기존 승인·취소 및 replay 회귀. 못 잡는 것: 실제 스캔 시간·외부 자원 접근.
- 실행: `uv run pytest tests/analysis/test_workflow.py`, `make check`, `make smoke-temporal`

## 완료 시 확인할 동작
로컬 Temporal에서 분석 단계가 fixture로 끝까지 돈다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리뷰 피드백으로 기존 02를 분석 골격(02)과 Sandbox 자식 Workflow(02B)로 분할.
- 2026-11-04(전체 재검토): 모델 이름을 `RequirementsV2`로 정정.
- 2026-11-04(공유 파일 경계): 분석 Workflow 연결에 필요한 `workflows/migration.py` 변경을 허용하고 기존 이력 replay·모델 직렬화 확인을 명시한다.
- 2026-11-04(연결 구체화): 분석 Worker 등록 경로와 기본 Converter의 JSON 호환 경계를 명시한다. 기존 이력은 변경 전 기록으로 replay한다.
- 2026-11-04(사용자 결정): 분석을 고정 ID의 자식 Workflow로 분리하고 부모 변경은 자식 연결·결과·실패·취소 처리에 한정한다.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
