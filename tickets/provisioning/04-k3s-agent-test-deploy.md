# Ticket 04 — 격리 K3s에서 Agent 배포 경로 검증

## 상태
DRAFT

## 목적
Agent가 기존 K3s의 격리된 테스트 Namespace에 DB·앱을 배포하고 상태를 보고한다.

## 관련 요구사항
PROV-006, PROV-009, PROV-011, PROV-013

## 선행 작업
Blocked by: 02, 03

## 작업 범위
- 포함: Agent 작업 ID·대상 클러스터/Namespace·Helm 산출물 참조, 연결·상태 조회와 테스트 리소스 정리를 구현한다.
- 제외: Production 승인 흐름과 외부 Envoy·WireGuard 구성

## 수용 기준
- [ ] Agent가 연결·클러스터 접근·StorageClass 가능 여부를 보고한다.
- [ ] 테스트 Namespace에서 DB PVC Bound·Pod Ready·앱 Service 연결을 확인한다.
- [ ] 동일 작업 ID 재전달과 Agent 재연결 시 기존 작업 상태를 조회한다.

## 테스트 전략
- 제안 Test Seam: Agent 작업 제출/조회 및 격리 K3s 상태. 클라우드 대상은 확인하지 못한다.
- 실행: Agent 상태 계약 테스트와 격리 K3s 통합 테스트; 리소스 정리 증거 기록; `make check`.

## 완료 시 확인할 동작
격리 K3s에서 실제 DB·앱 배포 결과와 작업 ID를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Agent 완료 통지 방식과 사용자 K3s 사전 준비 범위는 미확정이다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
