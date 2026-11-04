# Ticket Q-01 — `sandbox` Task Queue·Worker 등록

## 상태
DRAFT — 사용자 검토 전

## 목적
공유 파일(`queues.py`, `run.py`, `run_all.py`)을 건드리는 Queue 추가를 작은 PR로 먼저 반영해 이후 티켓의 충돌을 줄인다.

## 관련 요구사항
AV-021

## 선행 작업
Blocked by: D-05(Queue 분리 결정)

## 작업 범위
- 포함: `SANDBOX_QUEUE` 상수, 빈 `workers/sandbox.py` 등록, `run_all` 포함, 민서님 공유
- 제외: Sandbox Activity 구현(02B 이후)

## 수용 기준
- [ ] `SANDBOX_QUEUE`가 `cmp_contracts.queues`에 있고 기존 Queue 이름은 바뀌지 않는다.
- [ ] `run_all`이 sandbox Worker를 함께 띄운다.
- [ ] 기존 `make smoke-temporal`이 그대로 통과한다.
- [ ] PR 전에 민서님께 변경 파일을 공유한다.

## 테스트 전략
- 제안 Test Seam: Worker 기동과 기존 smoke 결과. 잡는 것: 등록 누락·회귀. 못 잡는 것: 실제 Activity 동작.
- 실행: `make check`, `make smoke-temporal`

## 완료 시 확인할 동작
로컬에서 sandbox Worker가 기존 Worker들과 함께 뜬다.

## 결정 대기
- D-05 Queue 분리 여부

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리뷰 피드백(공유 파일 충돌 위험)으로 신설.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
