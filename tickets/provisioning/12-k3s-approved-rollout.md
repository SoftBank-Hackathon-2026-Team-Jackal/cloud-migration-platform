# Ticket 12 — 승인된 K3s Agent 운영 배포

## 상태
DRAFT

## 목적
승인된 K3s 계획을 Agent에 보내고 기존 클러스터에서 DB·앱 준비를 확인한다.

## 관련 요구사항
PROV-009, PROV-015, PROV-016, PROV-017, PROV-018

## 선행 작업
Blocked by: 04A, 07

## 작업 범위
- 포함: Agent ID·작업 ID·Plan Digest 검사, Namespace·StorageClass·Helm 상태 재조회, 운영 배포 검증을 구현한다.
- 04A의 소유자·Agent 등록과 [공통 렌더 입력/출력](../../specs/provisioning/onprem-shared-deployment.md)을 사용한다. 원격 등록·차트 변환을 복제하지 않는다.
- 제외: 온프레미스 외부 중계·Envoy·WireGuard 생명주기

## 수용 기준
- [ ] 승인되지 않거나 변경된 Plan의 Agent 배포 요청을 거부한다.
- [ ] Agent가 오프라인이면 작업 상태를 보존하고 재연결 후 조회한다.
- [ ] PVC·DB·앱 상태와 내부 Endpoint를 확인하고 실패 시 `READY` 금지다.

## 테스트 전략
- 제안 Test Seam: 승인 Workflow → Agent 작업/실제 K3s 상태. 외부 트래픽 경로는 확인하지 못한다.
- 실행: 가짜 Agent의 중복/오프라인 계약 테스트와 격리 K3s 배포 검증; `make check`.

## 완료 시 확인할 동작
승인된 Plan에 연결된 K3s 운영 배포 상태를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Agent 작업 통지·인증, K3s 사전 준비와 외부 접근 인계 경계를 정한다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
