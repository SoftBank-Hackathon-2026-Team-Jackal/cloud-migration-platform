# Ticket Q-01 — `sandbox` Task Queue·Worker 등록

## 상태
IN_PROGRESS — 사용자 검토 완료, D-05 결정, Issue #32

## 목적
공유 파일(`queues.py`, `run.py`, `run_all.py`)을 건드리는 Queue 추가를 작은 PR로 먼저 반영해 이후 티켓의 충돌을 줄인다.

## 관련 요구사항
AV-021

## 선행 작업
Blocked by: 없음. D-05: Sandbox 전용 Queue 분리 결정.

## 작업 범위
- 포함: `SANDBOX_QUEUE` 상수, `workers/sandbox.py` 등록, `run.py` 단독 실행과 `run_all` 연결, 기동 검증용 무해한 scaffold Activity, 민서님 공유용 요약
- 제외: 실제 Sandbox Activity 구현(02B 이후), Workflow 연결(02·02B)

## 수용 기준
- [x] `SANDBOX_QUEUE`가 `cmp_contracts.queues`에 있고 기존 Queue 이름은 바뀌지 않는다.
- [x] `run_all`이 sandbox Worker를 함께 띄운다.
- [x] 단독 실행과 `run_all` 등록에서 Worker가 `SANDBOX_QUEUE`를 사용하며, pytest의 Temporal 테스트 환경에서 무해한 scaffold Activity 요청을 완료한다.
- [x] 기존 `make smoke-temporal`을 임시 Temporal 테스트 서버에서 추가 실행해 결과를 기록한다.
- [ ] 민서님께 공유할 변경 파일·영향 요약을 PR 설명에 정리한다. 전달은 사용자가 한다.

## 테스트 전략
- 합의된 Test Seam: Worker factory·단독 실행 및 `run_all` 등록을 테스트하고, pytest의 Temporal 테스트 환경에서 `sandbox` Queue의 무해한 scaffold Activity 완료를 확인한다. 잡는 것: Queue·등록 오류와 Task 전달. 못 잡는 것: 실제 Sandbox Activity·클러스터 동작.
- 실행: Q-01 전용 테스트와 `make check`가 기본이다. 테스트 서버 바이너리를 받지 못하면 그 사실과 미검증 항목을 기록한다. 로컬 Temporal이 있으면 `make smoke-temporal`을 추가 실행한다.

## 완료 시 확인할 동작
Temporal 테스트 환경에서 sandbox Worker가 해당 Queue의 Activity를 처리하고 기존 Worker 등록도 유지된다.

## 결정
- D-05: `sandbox` Queue를 기존 Queue와 분리한다. 기존 Queue 이름·역할은 유지한다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리뷰 피드백(공유 파일 충돌 위험)으로 신설.
- 2026-11-04(전체 재검토): 공유는 요약 준비까지로 조정(01과 같은 기준).
- 2026-11-04(사용자 결정): D-05 Queue 분리 및 직접 기동 검증을 확정했다. 실제 Sandbox 처리를 주장하지 않는 기동 전용 scaffold Activity를 명시한다.

## 검증 증거
- Ubuntu 24.04.5/WSL, Python 3.11.18, Helm 3.16.4: `make check` 통과(279 passed, 경고 1건).
- `tests/analysis/test_sandbox_queue.py`: Temporal 테스트 서버에서 Workflow → Sandbox Queue → scaffold Activity를 실행했고, 단독·전체 Worker 등록도 확인했다(3 passed).
- `make smoke-temporal`: 임시 Temporal 테스트 서버를 지정해 통과(`scaffold_complete`). 테스트 서버가 일부 heartbeat 기능을 알리지 않아 SDK 경고가 있었다. Docker Compose의 Temporal 1.29.1은 실행하지 않았고, 실제 Sandbox Activity·클러스터는 검증하지 않았다. 기존 smoke는 Sandbox Worker를 직접 기동하지 않으므로 해당 Queue 전달은 위 별도 테스트가 확인한다.

## 민서님 공유용 요약 초안
- `cmp_contracts.queues`에 `sandbox` Queue를 추가하고 `workers/run.py`·`workers/run_all.py`에서 해당 Worker를 등록한다. 기존 Queue 이름은 유지한다.
- `activities/sandbox.py`의 Activity는 Task 전달 확인용 scaffold이며, 실제 Sandbox 배포나 검증을 수행하지 않는다.
- Temporal 테스트 환경에서 실제 Queue Task 전달과 실행기 등록을 확인했다. 부모 Workflow는 변경하지 않았다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
