# Ticket 23b — 데모 AWS 연결 Launch Stack 링크 동작 (23 후속, 핫픽스)

## 상태
IN_PROGRESS — [Issue #205](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/205). 로컬 구현·검증과 두 스택의 변경 세트 생성까지 완료했다. 스택 실행, S3 업로드, 서버 `.env` 반영·릴리스, 실제 연결 verify는 사용자 승인 대기다.

## 목적
데모 서버에서 AWS 연결 생성 응답의 `launch_stack_url`이 `null`이다(점검 B-05). 링크가 생겨도 플랫폼 주체(인스턴스 역할)에 `sts:AssumeRole`이 없어 `POST /connections/{id}/verify`가 `ASSUME_ROLE_DENIED`가 된다(점검 B-02). 이 둘을 메워 사용자가 링크로 스택을 만들고 확인까지 할 수 있게 한다.

## 관련 요구사항
PROV-003, PROV-021, PROV-022

## 선행 작업
23(#118), 28(#152, 스택 `cmp-demo-build`), 41(#176, api 프로필·AWS 설정 파일 마운트). 코드(`apps/`·`packages/`) 변경 없음.

## 작업 범위
- 포함
  - compose api의 `CONNECTION_PLATFORM_PRINCIPAL_ARN`·`CONNECTION_TEMPLATE_BASE_URL`
  - 템플릿 전용 버킷 스택 `cmp-demo-connection-templates`
  - 빌드 스택의 AssumeRole 인라인 정책
  - 배포 스크립트 `deploy-connection-templates.sh`, 테스트, README·ADR-005·23 메모
- 제외
  - 생성 연결 프로필 파일 공유(`CONNECTION_AWS_CONFIG_FILE`, B-03 — 티켓 46 범위)
  - 연결 상세에 `platform_principal_arn` 노출(B-05 후반, OpenAPI·웹 합의 필요)
  - 실제 스택 실행·업로드·서버 반영(승인 대기)

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| H1 compose | api에만 `${CONNECTION_PLATFORM_PRINCIPAL_ARN:-}`·`${CONNECTION_TEMPLATE_BASE_URL:-}`. 비면 링크 `null`(이전 동작). **worker에는 넣지 않는다** | 링크는 api `routes/connections.py`만 만든다. worker 코드는 두 설정을 읽지 않는다(`grep`으로 확인). worker가 연결 역할을 쓰는 경로는 `credential_ref` 프로필 파일이고 46 범위다 |
| H2 버킷 | 새 전용 버킷 `cmp-demo-${AccountId}-apne2-connection-templates`(사용자 결정). 버킷 단위 BPA는 `BlockPublicAcls`·`IgnorePublicAcls`만 true, `BucketOwnerEnforced`, SSE-S3(익명 읽기는 KMS 키를 못 쓴다), 버전 관리 + 이전 버전 30일 만료, `Retain` | 기존 deploy 버킷에 공개 접두어를 여는 것보다 경계가 분명하다. 계정 단위 BPA 설정은 없다(`NoSuchPublicAccessBlockConfiguration`) |
| H3 버킷 정책 | `connections/${TemplateVersion}/*`만 익명 `s3:GetObject`. 모든 `s3:*`를 `aws:SecureTransport=false`면 거부. 쓰기·목록·ACL 없음. `TemplateVersion` 기본 `v1`, 형식 `^v[0-9]+$` | 템플릿에 비밀이 없다. 버전 경로라 이미 만든 사용자 스택이 가리키는 내용이 바뀌지 않는다(ADR-005) |
| H4 AssumeRole 위치 | 기존 별도 스택 `cmp-demo-build`에 **별도 인라인 정책** `cmp-demo-connections`(`ConnectionAssumePolicy`)로 추가. `cmp-demo-control-plane`은 건드리지 않는다 | 28 결정(EIP 때문에 control-plane 갱신 금지). 빌드 정책 `cmp-demo-build`의 범위·테스트를 그대로 둔다 |
| H5 AssumeRole 범위 | 리소스 `arn:${Partition}:iam::*:role/*`. 조건 셋 모두: `StringEquals iam:ResourceTag/cmp:managed-by = cloud-migration-platform`, `StringLike sts:ExternalId = cmp-*`, `Null sts:ExternalId = false` | 계정은 사용자 것이고 역할 이름은 CloudFormation이 만든다(스택 이름을 바꿀 수도 있어 이름 접두어로는 못 좁힌다). 태그는 두 연결 템플릿이 역할에 붙이는 값이고 테스트가 일치를 강제한다. 태그 조건이 없으면 같은 계정에서 계정 루트를 신뢰하는 아무 역할이나 Assume할 수 있다. 점검 B-02 제안은 `aws:ResourceTag`였지만 AWS 문서의 "태그 붙은 역할 Assume" 예시(교차 계정 포함)가 쓰는 `iam:ResourceTag`를 택했다. 정책 시뮬레이터로 태그+ExternalId만 `allowed`, 하나라도 없으면 `implicitDeny`를 확인했다 |
| H6 스크립트 | `deploy-build-stack.sh` 형식. 계정·git 깨끗함 확인 → 인스턴스 역할 이름(control-plane 스택 읽기)·`iam get-role` ARN 확인 → `--plan`이면 변경 세트만. apply면 스택 출력(버킷·URL) 확인 → 템플릿 2개를 `text/yaml`·`no-cache`로 업로드 → `curl -sI` 200 확인 → `.env` 두 줄 출력 | 같은 버전에 이미 있는 객체는 내용이 같으면 건너뛰고, 다르면 업로드하지 않고 실패한다("bump TemplateVersion"). 운영자가 값을 손으로 만들지 않게 한다 |

## 수용 기준
- [x] api가 두 설정을 서버 `.env`에서 받는다(빈 값이면 링크 `null`). worker는 받지 않는다 — compose 테스트
- [x] 버킷 공개 범위가 `connections/v1/*`의 `GetObject`뿐이고, ACL 공개·쓰기·목록이 없으며 비TLS를 거부한다 — 템플릿 테스트, cfn-lint
- [x] 인스턴스 역할이 태그·External ID 조건으로만 `sts:AssumeRole`을 갖는다. control-plane 스택은 변경 없음 — 템플릿 테스트, 정책 시뮬레이터(사용자 정의 정책)
- [x] 스크립트 `--plan`은 변경 세트만, apply는 업로드·공개 확인·`.env` 줄 출력, 바뀐 템플릿은 거부 — 가짜 `aws`·`git`·`curl` 테스트
- [ ] 실제 반영 후 생성 응답의 링크가 CloudFormation 빠른 생성 화면에 두 매개변수를 채우고, 스택 생성 → 역할 등록 → verify가 `CONNECTED` — 승인 대기

## 검증 기록 (2026-11-06)
- `tests/test_connection_launch_stack.py` (15): compose 두 설정·worker 제외, 링크 생성기 연결, 버킷 리소스·BPA·소유권·암호화·버전·보존, 버킷 정책(공개 1문장·TLS 거부·금지 동작 없음), 출력 URL, AssumeRole 문장·조건, 두 연결 템플릿의 태그·External ID 형식, 스크립트 `--plan`/apply/동일 객체 건너뜀/변경 거부/비공개 실패/control-plane 미접촉
- `tests/test_build_registry_runtime.py`: 빌드 스택 리소스 목록에 `ConnectionAssumePolicy` 추가(같은 역할)
- `make check`: 2020 passed, 9 skipped
- `cfn-lint` 1.60.0(`uvx`, 리전 ap-northeast-2): `connection-templates-stack.yaml`, `build-stack.yaml`, `infra/connections/*.yaml` 경고 없음. `aws cloudformation validate-template` 두 스택 통과
- `aws iam simulate-custom-policy`(새 문장): 태그+`cmp-…` ExternalId `allowed`, 태그 없음·ExternalId 없음·다른 형식 ExternalId 모두 `implicitDeny`
- 데모 EC2 `i-091464203346381b5`: IMDSv2 필수, hop limit 2 — api 컨테이너가 인스턴스 역할 자격 증명을 받을 수 있다
- 변경 세트(실행 안 함, 프로필 `cmp-aws-prod`, 계정 714066696240, ap-northeast-2)
  - `cmp-demo-build`(`CREATE_COMPLETE` 유지): `Add ConnectionAssumePolicy (AWS::IAM::RolePolicy)` 1건. 기존 ECR·`BuildAccessPolicy` 변경·교체 없음
  - `cmp-demo-connection-templates`(새 스택, `REVIEW_IN_PROGRESS`): `Add TemplateBucket (AWS::S3::Bucket)`, `Add TemplateBucketPolicy (AWS::S3::BucketPolicy)`

## 미검증 (사용자 승인 필요)
- 두 스택 실행, 템플릿 업로드, 서버 `.env` 두 줄 추가, 릴리스
- 조직 SCP가 공개 버킷 정책(`s3:PutBucketPolicy`)을 막는지는 실행해야 안다(SCP 원문은 읽을 권한이 없다, 점검 B-15)
- 교차 계정 `iam:ResourceTag` 평가는 시뮬레이터와 AWS 문서 예시로만 확인했다. 실제 verify가 `ASSUME_ROLE_DENIED`면 먼저 이 조건을 의심한다(대안: 태그 조건 제거 + ExternalId 조건만, 같은 계정 위험을 감수)
- 실제 연결: 링크로 스택 생성 → Role ARN 등록 → verify `CONNECTED`. 대상 계정은 서울 리전만 허용될 수 있다(B-15)

## 데모 서버 AssumeRole 경로에 더 필요한 것
- 연결 확인(verify)·기준점 조회는 이 티켓 반영으로 충분하다: api `CONNECTION_PLATFORM_AWS_PROFILE=cmp-demo-build`(41) → 인스턴스 역할 → 새 `cmp-demo-connections` 정책
- 분석 스캔·Provisioning이 `credential_ref`(`cmp-conn-<hex>`)를 쓰려면 생성 프로필 파일 공유(`CONNECTION_AWS_CONFIG_FILE`·`CONNECTION_AWS_BASE_CONFIG_FILE`, worker `AWS_CONFIG_FILE`)가 필요하다 — 티켓 46(#181) 범위
- 사용자가 템플릿 파일로 직접 만들 때 `PlatformPrincipalArn` 값을 보여 줄 응답 필드가 없다(B-05 후반, OpenAPI 변경)
- 사용자 역할 신뢰가 무작위 이름의 인스턴스 역할 ARN에 묶인다. control-plane이 역할을 다시 만들면 모든 연결이 끊긴다(B-16)

## 문서
README "환경 연결 Launch Stack", ADR-005 "남는 일", 티켓 23 "미검증" 메모, `specs/provisioning/tasks.md` 행. API 계약·정책 변화 없음.
