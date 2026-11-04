# Ticket 02B — Sandbox 자식 Workflow 골격

## 상태
DONE — 사용자 결정으로 완료, PR 병합 시 확정, Issue #45

## 목적
Sandbox 검증 루프를 자식 Workflow와 mock Activity로 끝까지 연결한다.

## 관련 요구사항
AV-014, AV-015, AV-016, AV-021

## 선행 작업
Blocked by: 02, Q-01

## 작업 범위
- 포함: `SandboxVerificationWorkflow`(mock 빌드·구성·렌더·적용·테스트·판정·분류), 최대 3회 루프, 사람 검토 Signal 대기, `publish_final_report` 자리, `finally` 정리, 진행 Query. `workflows/migration.py`의 자식 호출, `workers/migration.py`의 자식 Workflow 등록과 `workers/sandbox.py`의 Sandbox Activity 등록을 변경한다. 자식 Workflow는 기존 `WORKFLOW_QUEUE`, Activity는 `SANDBOX_QUEUE`를 쓴다.
- 제외: 실제 빌드·Helm 적용·LLM

## 수용 기준
- [x] mock 판정에 실패 1회를 주입하면 분류 → 수정 → 재시도 → 통과가 이력에 남는다(SPEC이면 빌드 없이, CODE면 재빌드 경로).
- [x] 실패를 3회 주입하면 `NEEDS_REVIEW`로 Signal을 기다리고 정리 Activity가 실행된다.
- [x] RENDER 분류면 수정 없이 중단되고 정리된다.
- [x] INFRA 분류면 수정 없이 같은 입력으로 재시도하고 시도 횟수가 늘지 않는다. 연속 2회면 사람 검토로 넘어간다.
- [x] Workflow 취소 시에도 정리 Activity가 실행된다.
- [x] 진행 Query가 현재 단계·시도 번호·판정을 돌려준다.
- [x] 기존 Workflow 이력과의 replay 호환성을 검토·검증하고 필요하면 안전한 버전 분기를 적용한다.
- [x] 부모의 자식 호출 변경 범위와 replay 결과를 PR 설명에 기록해 민서님께 공유한다.

## 테스트 전략
- 합의된 Test Seam: Temporal 테스트 환경(time skipping)의 결과·이력과 변경 전 부모 Workflow 이력 replay. 분기·검토 대기·취소·정리·Query 및 기존 이력 호환성을 확인한다. 실제 빌드·Helm·클러스터 동작과 외부 작업 시간·실패 형태는 검증하지 않는다.
- 02B 구현 전 02 적용 후 부모 이력을 저장하고 02 이전 이력도 replay한다. 별도 패치 ID를 사용한다. 취소 중에도 정리 Activity를 분리 취소 범위에서 실행한다.
- 실행: `uv run pytest tests/sandbox/test_workflow.py`, `make smoke-temporal`

## 완료 시 확인할 동작
분석 → Sandbox 루프 → 결과까지 mock으로 한 번에 돈다.

## 검증 증거
- 02B 구현 전 02 코드를 실행해 `tests/fixtures/analysis/migration-before-02b.json`에 56개 이벤트를 저장했다. 02 이전 완료·승인 대기 이력, 02 이후 이력과 02B 새 부모 이력 replay가 통과했다.
- `CMP_TEST_SERVER_PATH=<로컬 테스트 서버> uv run pytest tests/sandbox/test_workflow.py`: 16 통과. SPEC·CODE·BUILD·RENDER·INFRA 분기, 3회 실패·연속 INFRA 2회 검토, 조기 Signal 무시, Query, 부모 실패 차단, 취소와 지연된 정리 완료를 검사했다. INFRA에서 시도 번호가 변하지 않는 것을 이력으로 대조했다.
- macOS ARM64, Python 3.11, 임시 Helm 3.16.4, Temporal 테스트 서버: `make check` 418 통과(경고 3건). `make smoke-temporal`은 Sandbox Worker를 별도 실행한 상태에서 `scaffold_complete`를 반환했다. 기존 smoke 스크립트는 Sandbox Worker를 직접 띄우지 않으므로 별도 Worker 없이 실행하면 새 Workflow가 기다린다.
- 테스트 서버는 실제 Docker Compose Temporal 서버와 다르며, 강제 종료·Worker 장애 후 TTL 정리, 혼합 버전 Worker 배포는 확인하지 않았다. 실제 빌드·Helm·클러스터·데이터·LLM은 수행하지 않는다. mock의 `PASSED`는 실제 Sandbox 검증 증거가 아니다.
- 리뷰 반영: 알 수 없는 실패 분류는 `ApplicationError`(비재시도)로 Workflow를 실패시키고 정리는 실행된다. Sandbox Activity에 `schedule_to_start_timeout`(5분)을 둬서 Sandbox Worker가 없을 때 무한 대기하지 않게 했다. 테스트 서버가 schedule-to-start 타이머를 건너뛰지 않아 실제 만료는 검증하지 못하고, 이력에 제한값이 설정된 것만 확인한다. 추가 리뷰 반영: Sandbox 준비 전에 실패하면 정리를 실행하지 않고(없는 Sandbox를 위해 한 번 더 대기하지 않음), 정리 Activity가 실패해도 진행 중이던 최초 오류를 가리지 않으며 Query의 `cleanup_failed`로 표시한다. 실제 5분 제한으로 Worker 없이 장시간 실행한 검증은 하지 않았다.
- 팀 확인: 자식 분리 방향은 민서님이 확인했다. 부모의 `needs_review`·`stopped_render` 종료 값은 골격의 임시 동작이며, 검토 대기(진행 중 플래그)와 종료 상태(완료·실패·취소)를 구분하는 정리는 API 담당과 후속으로 진행한다.
- 부모는 비통과 결과에서 후속 scaffold 단계를 중단한다. 부모의 종료 상태 `needs_review`·`stopped_render`는 기존 API 스트림의 종료 상태 목록에 포함되지 않으며, API 변경은 담당자 협의 범위로 남는다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04(구현): `sandbox-child-v1`으로 부모를 분기하고 `sandbox-<migration_id>` 자식을 호출한다. 검토 전 정리하며, 조기 Signal은 무시하고 검토 종료는 통과로 취급하지 않는다. 실제 빌드·검증·발행·정리는 수행하지 않는다.
- 2026-11-04: 기존 02에서 분리.
- 2026-11-04(INFRA·D-07 반영): 환경 실패(INFRA)는 수정 없이 재시도하는 분기 추가.
- 2026-11-04(공유 파일 경계): 자식 호출·등록에 필요한 공유 Workflow 파일 변경을 허용하고 기존 이력 replay 확인을 명시한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
