# Ticket 04 — 매니페스트·클러스터 리소스 스캔

## 상태
DRAFT — 사용자 검토 전

## 목적
k8s 매니페스트에서 앱 실행 요구, 클라우드 의존 항목, 원본 DB 이미지, 변환 못 할 항목을 뽑는다.

## 관련 요구사항
AV-004

## 선행 작업
Blocked by: 01

## 작업 범위
- 포함: YAML 매니페스트·Helm 차트(`helm template` 후) 파싱, Deployment·Service·Ingress·ServiceAccount·PVC·StorageClass·ConfigMap·Secret 참조 추출, 클라우드 의존 감지, PostgreSQL Pod 이미지 Digest 기록, 변환 못 할 항목 목록
- 제외: 클러스터 직접 조회(06에서 같은 파서 재사용)

## 수용 기준
- [ ] ALB Ingress 어노테이션, IRSA 어노테이션, EBS StorageClass가 클라우드 의존으로 감지된다.
- [ ] CPU·메모리·replica·probe·포트가 근거 경로와 함께 나온다.
- [ ] PostgreSQL StatefulSet/Deployment의 이미지가 Digest(태그면 조회 필요 표시)와 함께 기록된다.
- [ ] 사이드카·initContainer·CRD·모르는 어노테이션이 변환 못 할 항목으로 나오고, ACK 등 AWS 리소스 CR은 AWS 전용 서비스로 분류된다.
- [ ] Secret은 이름·키만 읽고 `data` 값은 버린다.

## 테스트 전략
- 제안 Test Seam: `scan_manifests(path)` 결과. 잡는 것: 탐지 규칙. 못 잡는 것: 클러스터에서만 보이는 실제 상태.
- 실행: 데모 앱 축소 매니페스트 fixture, `uv run pytest tests/analysis/test_manifests.py`

## 완료 시 확인할 동작
fixture 매니페스트로 클라우드 의존 목록, 실행 요구, 원본 DB 이미지가 나온다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 원본 DB 이미지 우선 방침(D-11)에 따라 이미지 기록 추가.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
