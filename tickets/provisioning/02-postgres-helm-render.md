# Ticket 02 — PostgreSQL Chart 렌더링과 PVC 검사

## 상태
TODO — 범위 검토 완료; CP-01 및 아래 착수 조건 충족 후 구현

## 목적
공통 요구에서 PostgreSQL 16 StatefulSet·Service·PVC를 검증 가능한 Manifest로 만든다.

## 관련 요구사항
PROV-005, PROV-006, PROV-011

## 선행 작업
Blocked by: 01, CP-01

## 작업 범위
- 포함: 공통 Helm Chart/Values, DB 자원 요구, Secret 참조 자리와 StorageClass/PVC 렌더 검사를 구현한다.
- 제외: 실제 데이터 복사·CDC, Production 클러스터 설치

## 수용 기준
- [ ] 세 대상 Values로 PostgreSQL 16 StatefulSet·Service·PVC가 렌더된다.
- [ ] Pod CPU/메모리와 PVC 용량이 서로 다른 항목으로 반영되고 잘못된 참조는 실패한다.
- [ ] Sandbox Values는 Production 자격 증명 없이 같은 Chart를 렌더한다.

## 테스트 전략
- 제안 Test Seam: 공개 Helm 렌더 산출물. 실제 PVC Bound는 확인하지 못한다.
- 실행: `helm lint`, `helm template` 및 Manifest 구조 fixture; 가능하면 `make check`.

## 완료 시 확인할 동작
대상별 DB Manifest와 Sandbox 렌더 결과를 비교할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: DB/App 별도 Chart/Release로 결정. StorageClass·Secret 이름/키는 명시 입력이며 Provider 공통 기본값을 임의 지정하지 않는다.
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 순차 작업 조건 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)와 [공유 계약](../../specs/provisioning/shared-contracts.md)을 따른다. 사용자 후속 지시에 따라 한 티켓씩 순차 구현하고 관련 문서·SDD/index 수정은 동일 구현 이슈/PR에 포함한다. 공통 계약을 복제하지 않고 기존 모델을 사용한다.

담당: PostgreSQL Chart·Values 렌더·전용 테스트. Sandbox와 Production은 같은 DB Chart를 사용하고 서로 다른 Values를 전달한다.
