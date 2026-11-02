# Ticket 13 — 실제 검증 결과와 Migration 인계

## 상태
DRAFT

## 목적
배포 결과를 구조화해 Data/Traffic Migration이 필요한 연결 참조와 준비 상태를 소비하게 한다.

## 관련 요구사항
PROV-018, PROV-019, PROV-020, PROV-021

## 선행 작업
Blocked by: 12

## 작업 범위
- 포함: 인프라·워크로드·앱 검사 결과, `ProvisioningResult` 저장과 Worker 간 전달을 K3s 경로에서 구현한다. AWS/GCP 결과는 같은 계약의 fixture로 검증하고 실제 경로 연결은 통합 티켓에서 확인한다.
- 제외: 데이터 복사·CDC 실행과 트래픽 실제 전환

## 수용 기준
- [ ] DB Endpoint·인증 Secret 참조·앱 Endpoint·헬스 정보를 후속 단계에 전달한다.
- [ ] 필수 검사 실패면 결과에 실패 단계·원인을 남기고 다음 단계 진행을 막는다.
- [ ] 결과에 비밀번호 원문이 없고 Provisioning 완료를 데이터 이전 완료로 표시하지 않는다.

## 테스트 전략
- 제안 Test Seam: Provisioning Workflow 결과 → 후속 Worker 입력. 후속 Migration 자체의 성공은 확인하지 못한다.
- 실행: 준비/실패 결과 fixture와 Temporal 테스트 환경에서 후속 단계 인계·차단 확인; `make check`.

## 완료 시 확인할 동작
Data/Traffic Worker가 Secret 원문 없이 대상 접근 참조와 준비 상태를 읽는다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 결과의 외부 API 노출 범위와 기능 테스트 데이터 정책을 확정한다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
