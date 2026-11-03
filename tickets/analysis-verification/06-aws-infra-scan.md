# Ticket 06 — AWS 인프라·네트워크 스캔과 네트워크 의도

## 상태
DRAFT — 사용자 검토 전

## 목적
원본 AWS의 EKS·네트워크·IAM·CloudTrail을 읽기 전용으로 조회하고 Security Group·Ingress를 네트워크 의도로 바꾼다.

## 관련 요구사항
AV-005, AV-020

## 선행 작업
Blocked by: 01, AWS 테스트 계정(외부)

## 작업 범위
- 포함: `get_session(credential_ref)`(개발용 SSO 프로필), EKS·클러스터 리소스, VPC·Subnet·Route Table·SG, ELBv2, IRSA 역할 정책, CloudTrail LookupEvents, 네트워크 의도 변환, 스캔 시간 측정, 권한 목록 문서
- 제외: 비밀 값 조회, S3 데이터 이벤트

## 수용 기준
- [ ] `ingress → app:8080`, `app → db:5432`가 네트워크 의도로 나오고 원본 VPC CIDR이 기록된다.
- [ ] 권한 거부는 재시도 없이 필요한 권한 이름과 함께 실패한다.
- [ ] 스로틀링만 재시도한다.
- [ ] 비밀 값 조회 API를 호출하지 않는다(정책·코드 검사).
- [ ] 실제 계정에서 스캔 소요 시간을 기록하고, 권한 목록을 민서님께 전달한다.

## 테스트 전략
- 제안 Test Seam: `botocore.stub.Stubber`로 고정 응답을 준 스캐너 결과 + 실제 계정 1회 실행 기록. 잡는 것: 변환 규칙·오류 분류. 못 잡는 것: 다른 계정 구성.
- 실행: `uv run pytest tests/analysis/test_aws_scan.py`, 실제 계정 실행은 별도 기록

## 완료 시 확인할 동작
데모 EKS 환경을 스캔해 네트워크 의도와 리소스 설정이 나온다.

## 결정 대기
- AWS 테스트 계정 초대(소울님)
- SSM 사용 여부

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
