# Ticket 09 — AWS EKS에 DB·앱 배포 및 연결 확인

## 상태
DRAFT

## 목적
준비된 EKS에 PostgreSQL과 앱을 배포하고 데이터 이전 전 준비 상태를 검증한다.

## 관련 요구사항
PROV-010, PROV-011, PROV-012, PROV-013, PROV-018

## 선행 작업
Blocked by: 02, 03, 08

## 작업 범위
- 포함: Secret 주입, DB·앱 Helm Release, EBS PVC·Service·ALB Ingress와 헬스/DB 연결 확인을 구현한다.
- 제외: 데이터 이전·트래픽 가중치 변경

## 수용 기준
- [ ] DB PVC Bound·접속과 앱 Pod Ready·헬스를 별도로 확인한다.
- [ ] ALB Ingress 주소와 실제 외부 접근 경로를 확인한다.
- [ ] 이미지 Pull·Secret 참조·PVC 오류가 있으면 `READY`를 반환하지 않는다.

## 테스트 전략
- 제안 Test Seam: EKS 배포 요청 → Kubernetes/Ingress/앱 준비 결과. 데이터 정합성은 확인하지 못한다.
- 실행: Helm 렌더/Release 재조회와 격리 EKS 통합 검증; `make check` 및 `helm lint`.

## 완료 시 확인할 동작
EKS에서 실제 DB·앱 준비와 Endpoint를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: ECR Pull 권한, TLS/도메인 및 Production 기능 테스트 범위를 결정한다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
