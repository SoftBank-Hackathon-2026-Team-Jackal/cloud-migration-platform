# Ticket 02 — 분석 Activity 골격

## 상태
DONE — 사용자 결정에 따라 02B 진행, Issue #44. 민서님 공동 리뷰와 CI 결과 확인은 남음.

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
- [x] 스캔 4종이 병렬로 실행되는 것이 이력에서 보인다.
- [x] 분석 결과로 01의 `AnalysisReport`·`RequirementsV2` fixture가 반환된다.
- [x] Workflow 코드에 외부 I/O·시각·난수가 없다(Temporal 샌드박스 통과).
- [x] 기존 승인 대기 스캐폴드 흐름이 깨지지 않는다.
- [x] 부모가 분석 자식의 완료·실패·취소를 처리한다. 공유 부모 파일의 변경 범위와 replay 결과를 PR 설명에 기록해 민서님께 공유한다.
- [x] 기존 진행 중 Workflow 이력과의 replay 호환성을 검토·검증하고 필요하면 안전한 버전 분기를 적용한다(검증한 이력의 범위는 아래 참조).
- [x] 기존 Temporal 기본 Data Converter를 유지한다. Activity 경계에서 Pydantic 모델을 `model_dump(mode="json")`·`model_validate`로 처리하고, Workflow·Activity 경계의 fixture 왕복과 잘못된 입력 거절을 실제 Converter로 검증한다. 이력에는 참조·작은 메타데이터만 담는다.

## 테스트 전략
- 합의된 Test Seam: Temporal 테스트 환경의 Workflow 결과·이력과 실제 기본 Data Converter, 변경 전 Workflow 이력 replay. 잡는 것: 순서·병렬·계약 직렬화, 기존 승인·취소 및 replay 회귀. 못 잡는 것: 실제 스캔 시간·외부 자원 접근.
- 구현 전 기존 Workflow를 실행해 JSON 이력을 저장한다. 이력에서 자식 ID `analysis-<migration_id>`와 Workflow Queue `migration`을 확인하고, 가짜 `.env` 비밀값과 비밀 형태의 문자열이 이력에 없는지 검사한다.
- 자식 실패는 부모 실패로 전파해 후속 단계로 진행하지 않는다. 부모의 `request_cancel` Signal은 자식 취소를 요청하고 완료를 기다린 뒤 `cancelled`를 반환한다. 부모의 외부 취소 시에도 자식 취소를 요청한다. 부모가 그 밖의 이유로 종료될 때 남은 자식은 `ParentClosePolicy.TERMINATE`로 종료한다.
- 실행: `uv run pytest tests/analysis/test_workflow.py`, `make check`, `make smoke-temporal`

## 완료 시 확인할 동작
로컬 Temporal에서 분석 단계가 fixture로 끝까지 돈다.

## 검증 증거 (진행 중)
- 변경 전 Workflow를 Temporal 테스트 서버에서 실행해 51개 이벤트를 `tests/fixtures/analysis/migration-before-02.json`에 보관했다. 변경 후 이력 replay가 통과했다.
- `CMP_TEST_SERVER_PATH=<로컬 테스트 서버> uv run pytest tests/analysis/test_workflow.py -q`: 10 통과. 동시 이전의 자식 ID·Queue·ParentClosePolicy, 스캔 병렬 스케줄, fixture 모델 검증, 잘못된 요구사항 거절, 가짜 `.env` 값 비노출, 실패 전파, Signal과 외부 취소의 경합을 확인했다. 변경 전 승인 대기 이력과 패치가 있는 새 이력도 replay했다.
- macOS ARM64, Python 3.11, 임시 Helm 3.16.4와 Temporal 테스트 서버: 리뷰 수정 후 `make check` 402 통과(경고 3건). `make smoke-temporal`은 테스트 서버에서 `scaffold_complete`를 반환했다. Docker Compose의 실제 Temporal 서버는 사용하지 않았다.
- 초기 테스트에서 외부 Workflow 취소 후 자식이 남는 것을 확인해 명시적인 자식 취소 요청을 추가했다. 테스트 서버에서 부모와 자식의 취소를 확인한다. 부모가 강제 종료되는 경우의 실제 서버 ParentClosePolicy 실행은 별도로 검증하지 않았다.
- 자식 실패 후 Query의 phase는 기존 scaffold의 Activity 실패와 같이 `analysis`로 남는다. 실패 상태의 외부 API 표현은 이번 공유 Workflow 변경 범위 밖이며, 이 값만으로 실행 성공·진행 중 여부를 판단할 수 없다. GitHub Actions에서는 테스트 서버 자동 다운로드를 사용하므로 실제 CI 네트워크 성공 여부는 미확인이다.
- 구·신 Worker의 혼합 버전 배포와 모든 가능한 진행 중 Workflow 이력의 replay는 검증하지 않았다. 민서님 공동 리뷰와 PR 설명·CI 확인은 남아 있다.
- 실스캔·클라우드 접근·LLM은 범위 밖이다. 가짜 `.env` 값 검사만으로 임의 문자열의 비밀 여부를 판별하지는 못한다. Activity의 실제 오류별 재시도 분류와 스캔 heartbeat는 후속 실구현에서 검증한다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04(리뷰): migration ID를 부모 Workflow ID로 통일하고 자식 취소 대기 중 외부 취소가 삼켜지거나 중복 요청되지 않도록 수정했다. 사용하지 않는 `analysis_ref`는 제거했다.
- 2026-11-04: 리뷰 피드백으로 기존 02를 분석 골격(02)과 Sandbox 자식 Workflow(02B)로 분할.
- 2026-11-04(전체 재검토): 모델 이름을 `RequirementsV2`로 정정.
- 2026-11-04(공유 파일 경계): 분석 Workflow 연결에 필요한 `workflows/migration.py` 변경을 허용하고 기존 이력 replay·모델 직렬화 확인을 명시한다.
- 2026-11-04(연결 구체화): 분석 Worker 등록 경로와 기본 Converter의 JSON 호환 경계를 명시한다. 기존 이력은 변경 전 기록으로 replay한다.
- 2026-11-04(사용자 결정): 분석을 고정 ID의 자식 Workflow로 분리하고 부모 변경은 자식 연결·결과·실패·취소 처리에 한정한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
