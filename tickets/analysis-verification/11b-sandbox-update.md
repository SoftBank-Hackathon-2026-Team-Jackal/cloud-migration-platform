# Ticket 11B — Sandbox 제자리 갱신과 테스트 데이터 초기화

## 상태
DRAFT — 사용자 검토 전

## 목적
시도마다 Sandbox를 새로 만들지 않고 바뀐 부분만 갱신하며, 테스트 데이터를 처음 상태로 되돌린다.

## 관련 요구사항
AV-011, AV-015

## 선행 작업
Blocked by: 11

## 작업 범위
- 포함: 갱신 규칙(이미지만 → 앱 Release 갱신, 설정·바인딩 → 재렌더, env Secret 내용 → Pod 재시작 보장, 바꿀 수 없는 필드·DB 메이저 버전 → 해당 Release 재생성), `reset_test_data`, 시도별 Digest·렌더 결과 기록
- 제외: 실패 분류·수정(14)

## 수용 기준
- [ ] 이미지 Digest만 바꾸면 앱 Release만 갱신되고 DB는 그대로다.
- [ ] env Secret 내용만 바꿔도 앱 Pod가 새 값으로 다시 뜬다.
- [ ] 시도마다 테스트 데이터가 처음 상태로 돌아간다.
- [ ] 시도별 Digest·Values SHA-256이 기록된다.

## 테스트 전략
- 제안 Test Seam: 격리 k3d에서 갱신 전후 클러스터 상태. 잡는 것: 갱신 범위·재시작·초기화. 못 잡는 것: 운영 데이터 규모의 마이그레이션 시간.
- 실행: `uv run pytest tests/integration/sandbox -m k3d -k update`

## 완료 시 확인할 동작
같은 Sandbox에서 이미지 교체와 설정 변경을 연속으로 적용해도 테스트가 같은 시작 상태에서 돈다.

## 결정 대기
- 앱 차트의 Secret 내용 해시 어노테이션 지원 여부(없으면 rollout restart)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리뷰 피드백으로 기존 11에서 분리.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
