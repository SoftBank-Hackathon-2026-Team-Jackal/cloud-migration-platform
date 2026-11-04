# Ticket 06 — AWS 인프라·네트워크 스캔과 통신 의도

## 상태
DRAFT — 사용자 검토 전

## 목적
원본 AWS의 EKS·네트워크·IAM·CloudTrail을 읽기 전용으로 조회하고 Security Group·Ingress를 `RequirementsV2`의 `network_intent` 목록과 외부 진입 정보로 바꾼다.

## 관련 요구사항
AV-005, AV-020

## 선행 작업
Blocked by: 01, AWS 테스트 계정(외부)

## 작업 범위
- 포함: `get_session(credential_ref)`(개발용 SSO 프로필), EKS·클러스터 리소스, VPC·Subnet·Route Table·SG, ELBv2, IRSA 역할 정책, CloudTrail LookupEvents, `network_intent` 목록(`{source_resource_id, destination_resource_id 또는 external_host, port, protocol}`) 변환, 외부 진입·원본 CIDR은 `AnalysisReport`에 기록, 스캔 시간 측정, 권한 목록 문서
- 제외: 비밀 값 조회, S3 데이터 이벤트

## 수용 기준
- [ ] fixture의 앱 → DB 5432 통신이 `network_intent` 항목으로 나오고 `RequirementsV2` 검증을 통과한다.
- [ ] 외부 진입(443 → 앱 8080, ALB·CloudFront 등 진입 서비스)과 원본 VPC CIDR이 `AnalysisReport`에 기록된다.
- [ ] 대상 CIDR·Security Group ID 같은 환경 값이 intent에 들어가지 않는다.
- [ ] 권한 거부는 재시도 없이 필요한 권한 이름과 함께 실패한다. 스로틀링만 재시도한다.
- [ ] 비밀 값 조회 API를 호출하지 않는다(정책·코드 검사).
- [ ] 실제 계정에서 스캔 소요 시간을 기록하고, 민서님께 넘길 권한 목록을 정리한다. 전달은 사용자가 한다.

## 테스트 전략
- 제안 Test Seam: `botocore.stub.Stubber`로 고정 응답을 준 스캐너 결과 + 실제 계정 1회 실행 기록. 잡는 것: 변환 규칙·오류 분류. 못 잡는 것: 다른 계정 구성.
- 실행: `uv run pytest tests/analysis/test_aws_scan.py`, 실제 계정 실행은 별도 기록

## 완료 시 확인할 동작
데모 EKS 환경을 스캔해 `network_intent`와 리소스 설정이 나온다.

## 결정 대기
- AWS 테스트 계정 초대(소울님)
- SSM 사용 여부

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: v2 동기화로 intent 형식을 `RequirementsV2.network_intent`로 변경. 외부 진입 표현은 D-15.
- 2026-11-04(전체 재검토): 특정 앱 이름을 일반 표현으로 바꾸고, 공유는 정리까지로 조정.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
