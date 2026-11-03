# Ticket 11 — Sandbox 적용·갱신·초기화·정리

## 상태
DRAFT — 사용자 검토 전

## 목적
공통 Chart로 Sandbox를 띄우고 시도마다 제자리 갱신하며, 끝나면 반드시 정리한다.

## 관련 요구사항
AV-011, AV-016, AV-020

## 선행 작업
Blocked by: 02, Provisioning 02·03(DONE)

## 작업 범위
- 포함: 로컬 k3d Sandbox(`cmp-sandbox`), 임시 이름 규칙, 가짜 DB Secret 생성, `PostgresChartValues`·`prepare/resolve/render_application`으로 렌더, `helm upgrade --install --wait`, 갱신 규칙, 재생성 분기, 임시 준비 검사, `reset_test_data`, Namespace 삭제 정리, 실행 ID·만료 라벨, 만료 정리 작업
- 제외: 내부 Ingress·NetworkPolicy(결정 대기), GKE/EKS Sandbox

## 수용 기준
- [ ] 첫 적용 후 DB·앱 Pod Ready, SQL 접속, 앱 헬스가 통과한다.
- [ ] 이미지 Digest만 바꾸면 앱 Release만 갱신되고 DB는 그대로다.
- [ ] 시도마다 테스트 데이터가 처음 상태로 돌아간다.
- [ ] 통과·실패·취소 모두에서 Namespace와 보존 PVC까지 지워진다.
- [ ] 시도별 Digest·Chart 버전·Values SHA-256이 기록된다.
- [ ] 가짜 비밀번호가 로그·결과·이력에 없다.

## 테스트 전략
- 제안 Test Seam: 격리 k3d + Helm 3.17.3에서 `apply_sandbox`·`teardown_sandbox` 결과와 클러스터 상태. 잡는 것: 실제 적용·갱신·정리. 못 잡는 것: 클라우드 StorageClass·Ingress Controller 차이.
- 실행: `uv run pytest tests/integration/sandbox -m k3d`, `make check`

## 완료 시 확인할 동작
fixture 이미지로 Sandbox가 뜨고, 이미지 교체 갱신 후 정리까지 끝난다.

## 결정 대기
- 통합 Sandbox 렌더 함수 제공 여부(이름 규칙·준비 검사 포함)
- 앱 Chart의 내부 Ingress·NetworkPolicy 렌더

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
