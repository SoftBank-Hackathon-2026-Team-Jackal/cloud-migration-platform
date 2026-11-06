# Ticket 06 — AWS 인프라·네트워크 스캔과 통신 의도

## 상태
IN_PROGRESS — PR #90 병합, Issue #89. 실제 AWS 계정 1회 실행(승인 필요) 기록 후 DONE

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
- [x] fixture의 앱 → DB 5432 통신이 `network_intent` 항목으로 나오고 `RequirementsV2` 검증을 통과한다.
- [x] 외부 진입(443 → 앱 8080, ALB·CloudFront 등 진입 서비스)과 원본 VPC CIDR이 `AnalysisReport`에 기록된다.
- [x] 대상 CIDR·Security Group ID 같은 환경 값이 intent에 들어가지 않는다.
- [x] 권한 거부는 재시도 없이 필요한 권한 이름과 함께 실패한다. 스로틀링만 재시도한다.
- [x] 비밀 값 조회 API를 호출하지 않는다(정책·코드 검사).
- [ ] 실제 계정에서 스캔 소요 시간을 기록하고, 민서님께 넘길 권한 목록을 정리한다. 전달은 사용자가 한다.

## 테스트 전략
- 합의된 Test Seam: `botocore.stub.Stubber`로 고정 응답을 준 스캐너 결과 + 실제 계정 1회 실행 기록. 잡는 것: 변환 규칙·오류 분류. 못 잡는 것: 다른 계정 구성.
- 실행: `uv run pytest tests/analysis/test_aws_scan.py`, 실제 계정 실행은 별도 기록

## 완료 시 확인할 동작
데모 EKS 환경을 스캔해 `network_intent`와 리소스 설정이 나온다.

## 결정 대기
- AWS 테스트 계정 초대(소울님)
- SSM 사용 여부

## 검증 증거 (가짜 AWS 응답)
- `uv run pytest tests/analysis/test_aws_scan.py tests/analysis/test_network.py`: 30 통과.
  - 스캔(`cmp_scanners.aws_infra`, `botocore.stub.Stubber`): EKS 클러스터, 클러스터 VPC의 CIDR·서브넷·라우트 테이블(인터넷 경로 여부)·보안 그룹 규칙, ELBv2(리스너와 대상 그룹 포트), IRSA 역할 정책의 동작 목록, CloudTrail 관리 이벤트 요약(출처·이름·횟수만, 사용자 정보 없음), 다른 VPC 자원 제외, 스캔 시간 측정.
  - 오류: 권한 거부(`UnauthorizedOperation` 등)는 재시도 없이 `PERMISSION_DENIED`와 필요한 IAM 동작 이름(예: `ec2:DescribeSecurityGroups`)으로 실패, 스로틀링만 0.5·1·2초 backoff로 재시도하고 4회 모두 실패하면 `THROTTLED`, 그 밖의 오류는 AWS 메시지 없이 `AWS_ERROR`. 오류 문자열에 AWS 응답 문구가 없음.
  - 읽기 전용: 호출된 연산이 모두 허용 목록(`ALLOWED_OPERATIONS`)의 Describe·List·Get·Lookup이고 `Secret`·`Parameter`·`Decrypt`·`Create`·`Delete`·`Put`·`Update`가 없음을 호출 기록으로 확인하고, 소스에 비밀 저장소(`secretsmanager`, `ssm`, `kms` 등) 문자열이 없음을 코드 검사로 확인. 프로필 이름이 아닌 값(액세스 키, 공백, 80자 초과, 빈 값)은 `get_session`이 거절하고 기본 프로필을 쓰지 않음.
  - 통신 의도(`cmp_scanners.network`): 보안 그룹 규칙(그룹 간, 단일 포트)이 앱 → DB 5432 `network_intent`로 나오고 07의 `RequirementsV2` 검증을 통과, 의도에 CIDR·보안 그룹 ID가 없음, 인터넷 노출 로드 밸런서(HTTP·HTTPS 리스너)가 외부 진입(`ALB`)으로 `AnalysisReport`용 `ExternalEntry`에 담기고 VPC CIDR이 `source_cidrs`로 나옴, 로드 밸런서가 없으면 인터넷에 열린 보안 그룹을 외부 진입으로 보고, 있으면 중복하지 않음, 대상이 모호하면 추측하지 않고 `EXTERNAL_ENTRY_TARGET_AMBIGUOUS`로 보고, 넓은 규칙(`-1`·포트 범위)·매핑되지 않는 그룹은 건너뛰고 기록, 코드의 문자열 외부 호스트가 앱별 `external_host` 의도로 나옴(스킴·명시 포트로 80·443 추론, 클러스터 내부 주소 제외, Go 어댑터 출력과 연결).
- 추가 변경: Go 어댑터가 외부 호스트의 URL 스킴을 `CallSite.detail`에 남긴다(포트 추론용). `boto3`를 런타임 의존성으로 추가했다(`pyproject.toml`, `uv.lock`에는 boto3·botocore·s3transfer·jmespath·python-dateutil·six·urllib3만 추가, `--locked` 동기화 통과).
- Helm이 필요한 테스트 파일을 제외한 `pytest`와 `ruff check`·`ruff format --check` 통과.
- 문서 변경: spec·plan 변경 없음(§2.2·AV-005와 같은 구조).

## 스캔 권한 목록 (민서님께 넘길 읽기 전용 정책, 전달은 사용자가 한다)
`cmp_scanners.aws_infra.required_iam_actions()`가 만든다. `scan_policy_document()`가 같은 내용의 IAM 정책 JSON(`Effect: Allow`, `Resource: *`)을 돌려준다.

```text
cloudtrail:LookupEvents
ec2:DescribeRouteTables
ec2:DescribeSecurityGroups
ec2:DescribeSubnets
ec2:DescribeVpcs
eks:DescribeCluster
elasticloadbalancing:DescribeListeners
elasticloadbalancing:DescribeLoadBalancers
elasticloadbalancing:DescribeTargetGroups
iam:GetPolicy
iam:GetPolicyVersion
iam:GetRolePolicy
iam:ListAttachedRolePolicies
iam:ListRolePolicies
```
비밀 값 조회 권한은 포함하지 않는다. S3 데이터 이벤트는 읽지 않는다.

## 한계와 미검증
- **실제 AWS 계정에서 실행하지 않았다.** 수용 기준 중 "실제 계정에서 스캔 소요 시간을 기록하고 권한 목록을 정리한다"의 실행 기록이 남아 있다. AGENTS.md상 `cmp-aws-prod`는 운영 이름의 환경이므로 사용자 승인과 읽기 전용 호출만으로 1회 실행해 소요 시간과 실제 응답 모양(특히 보안 그룹·ELB 형태)을 기록해야 한다. 이 티켓은 그 기록 전까지 `DONE`으로 바꾸지 않는다.
- 보안 그룹 ↔ 워크로드 연결은 보안 그룹 이름·태그에 리소스 ID가 들어 있다는 규칙으로 추정한다. 이름이 다르면 연결하지 못하고 기록만 한다. EKS Pod의 보안 그룹(SecurityGroupPolicy)·노드 그룹 보안 그룹은 읽지 않는다.
- 로드 밸런서의 대상은 앱이 하나일 때만 자동으로 정하고, 여러 개면 이름으로 맞춘다. 맞지 않으면 기록만 한다. CloudFront 등 다른 진입 서비스는 읽지 않는다.
- CloudTrail은 최근 N일의 관리 이벤트 요약이다. 최근 이벤트 500개(10페이지)까지만 0.5초 간격으로 읽고, 이 조회만 실패하면 `cloudtrail_status=NOT_COLLECTED`로 두고 나머지 스캔은 유지한다(2026-11-07 결선 데모 스로틀 수정). 코드와의 교차 해석(확실히 사용·드문 경로·미사용 권한)은 이 티켓 범위 밖이다.
- 02의 `scan_aws_infra`·`scan_network` Activity는 mock 그대로이고, 07의 `network_intent`와 병합하는 연결(`merge_network_intents` 사용)과 `AnalysisReport`에 외부 진입·원본 CIDR을 넣는 통합은 별도 작업이다.
- 자격 증명 참조(`get_session(credential_ref)`)는 개발용 프로필 이름이다. 민서님 환경 연결이 발급하는 참조로의 교체는 후속이다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: v2 동기화로 intent 형식을 `RequirementsV2.network_intent`로 변경. 외부 진입 표현은 D-15.
- 2026-11-04(전체 재검토): 특정 앱 이름을 일반 표현으로 바꾸고, 공유는 정리까지로 조정.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
