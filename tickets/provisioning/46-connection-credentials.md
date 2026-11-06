# Ticket 46 — 연결 `credential_ref`로 실행하고 플랫폼 주체 권한·연결 설정 갖추기 (23·ADR-005 후속)

## 상태
IN_PROGRESS — [Issue #181](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/181). 로컬 구현·검증 완료. 플랫폼 주체 권한·템플릿 호스팅·Launch Stack 설정은 핫픽스 [23b](23b-connection-launch-stack-hotfix.md)(#205, PR #208)가 AWS·서버에 먼저 반영했고, 이 티켓은 그 설계(주체 = 인스턴스 역할) 위에 맞췄다. 리뷰, 서버 릴리스(공유 연결 파일), 실계정 연결 확인·교차 계정 Plan, 템플릿 v2(B-12·B-14) 반영 결정이 남았다.

## 배경 (점검 B-01·B-02·B-03·B-05·B-12·B-13·B-14·B-16, A-12, 재검사 D-20)
- Provisioning이 Plan 입력의 `credential_ref`를 읽지 않고 worker 고정 프로필(`PROVISIONING_AWS_PROFILE`) 하나로 Plan·Apply·kubectl을 실행했다(`aws_preview.py:73`은 정의만 있음). ADR-005("Provisioning `aws_profile`이 `credential_ref`를 그대로 받는다")와 달랐다.
- 데모 인스턴스 역할에 `sts:AssumeRole`이 없어 연결 확인이 늘 `ASSUME_ROLE_DENIED`였다(`simulate-principal-policy` implicitDeny).
- 생성 연결 프로필 파일(`CONNECTION_AWS_CONFIG_FILE`)이 api·worker 어디에도 연결되지 않아 `cmp-conn-*`를 아무도 해석하지 못했다.
- 템플릿 S3 호스팅·`CONNECTION_PLATFORM_PRINCIPAL_ARN`·`CONNECTION_TEMPLATE_BASE_URL`이 없어 사용자가 스택 링크·주체 ARN을 받지 못했다.
- 대상 역할의 IAM 이름 조건(`cmp-`)과 `cluster_name` 검증이 어긋났고(B-12), 교차 계정이면 State backend가 대상 역할로 접근했고(B-13), SG 규칙·EKS 태그 생성이 템플릿 조건을 통과하는지 미검증이었고(B-14), 사용자 신뢰가 무작위 이름의 인스턴스 역할에 묶여 있었다(B-16).
- D-20: AWS 자격 증명이 없을 때 계정 확인 실패가 `PLAN_COMMAND_FAILED`로 뭉뚱그려졌다(`aws_preview.py:353-361`).

## 관련 요구사항
PROV-003, PROV-007, PROV-021, PROV-022

## 선행 작업
Blocked by: 23(#118, 연결 API), 41(#176, api 플랫폼 프로필). 44(#179, 웹 시작 요청)는 이 티켓이 소비하는 기존 계약 필드(`PlanningSettings.target.settings.credential_ref`)를 채운다. 순서 의존은 없다(필드가 연결 ID가 아니면 지금처럼 고정 프로필).

## 작업 범위
- 포함: `credential_ref`(연결) → 연결 프로필 해석과 Plan·Apply·AWS 워크로드 실행별 전달, State backend 플랫폼 프로필 분리, Plan 시점 연결 상태 검사, 계정 확인 실패 코드·단계 이름(D-20), 연결 경로 `cluster_name` 접두어 검증(B-12 코드 쪽), API 시작 시 생성 프로필 파일 재작성, 연결 생성·상세 응답의 `platform_principal_arn`, compose·`ensure-secrets.sh` 공유 파일 연결(B-03), ECR 토큰 프로필 분리, 문서.
- 23b로 옮김(오케스트레이터 결정, 2026-11-06): 플랫폼 주체 `sts:AssumeRole` 권한(B-02), 템플릿 S3 호스팅·`CONNECTION_PLATFORM_PRINCIPAL_ARN`·`CONNECTION_TEMPLATE_BASE_URL`(B-05).
- 후속으로 남김: B-16(아래 C8), 템플릿 v2(아래 C15).
- 제외: AWS 대상 리소스 권한 자체·교차 계정 ECR pull(47), GCP 주체(48), 웹 시작 요청(44), 웹 표시(별도 저장소, 협의 필요), 분석 스캔의 연결 프로필 사용(분석 담당, 협의 필요), 원격 변경 전부.

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| C1 `credential_ref` 형식 | AWS Plan 입력 `credential_ref`(기존 `VersionRef`)의 `id`가 연결 프로필 이름 `cmp-conn-<12 hex>`(ADR-005의 `credential_ref`, 44 시작 요청이 넣는 값) 또는 연결 ID `conn-<12 hex>`면 대상 연결이다. 프로필은 `cmp-conn-<12 hex>`. `version`은 `"1"`(Role ARN은 한 번만 등록). 두 접두어로 시작하지만 형식이 틀리면 `INVALID_CREDENTIAL_REF`. 계약 필드 추가 없음 | 기존 필드를 그대로 쓴다(`shared-contracts.md` 우선). 44(main 병합)가 `cmp-conn-<hex>`를 쓰므로 그 값을 받아야 한다. 정해진 형식만 받고 Plan 전에 DB로 연결을 확인하므로(C5) 입력이 worker의 다른 프로필을 고를 수 없다 |
| C2 연결이 없을 때 | `id`가 연결 ID가 아니면(예: 지금 데모 `credential-001`) **지금처럼** worker `PROVISIONING_AWS_PROFILE` 하나로 provider·State 모두 실행한다(데모 같은 계정 호환) | 44 이전 시작 요청과 기존 저장 Plan(spec.json)이 그대로 동작해야 한다. 고정 프로필을 금지하면 데모 같은 계정 경로가 47 전에 막힌다 |
| C3 해석 위치 | 순수 함수 `cmp_providers.aws_credentials.resolve_aws_profiles(credential_ref, platform_profile=)` → `AwsProfiles(provider, state, connection_id)`. Plan(`plan_aws_infrastructure`)은 요청에서, Apply(`apply_aws_infrastructure`)는 **승인된 spec.json**에서, AWS 워크로드(`AwsWorkloadsActivities`)는 **승인된 WORKLOADS spec 산출물의 `target.settings`**에서 해석한다. 세 함수의 `aws_profile` 인자는 이제 "플랫폼 프로필(State·기본값)" 뜻이다 | 실행별 값이 승인 대상 바이트에 고정된다(Plan과 다른 연결로 Apply할 수 없다). worker 전역 설정을 바꾸지 않아 동시 실행이 섞이지 않는다. 47·48은 이 함수와 `AwsProfiles`를 경계로 쓴다 |
| C4 State backend (B-13) | S3 backend `terraform init`에 `-backend-config=profile=<플랫폼 프로필>`을 항상 준다. provider·`aws sts`·`aws eks`는 `AWS_PROFILE=<연결 프로필>` | State 버킷은 플랫폼 계정에 있다. GCP 경로가 이미 이렇게 분리돼 있다. 같은 계정 경로는 두 값이 같아 동작이 같다 |
| C5 Plan 시점 연결 검사 | `PlanPublisher`가 AWS Plan 전에 `cmp_api.connections.check_target_connection`으로 연결을 확인한다: 있음, `AWS_TARGET`, Role 등록, `CONNECTED`, 확인된 계정 = `account_id`. 실패는 `PLAN_FAILED` + `TARGET_CONNECTION_NOT_FOUND`·`TARGET_CONNECTION_KIND_MISMATCH`·`TARGET_CONNECTION_NOT_VERIFIED`·`TARGET_CONNECTION_ACCOUNT_MISMATCH` | Terraform을 돌리기 전에 원인을 알려 준다. Apply는 DB를 다시 보지 않는다 — 승인 뒤 연결을 지우면 프로필이 사라져 `ACCOUNT_CHECK_FAILED`로 끝난다(재Plan 필요) |
| C6 D-20 | 계정 확인 명령 실패는 `AWS_IDENTITY_UNAVAILABLE`(응답 해석 실패는 기존 `ACCOUNT_CHECK_FAILED`). 모든 명령 실패에 단계 이슈 `PLAN_STEP_IDENTITY`·`PLAN_STEP_INIT`·`PLAN_STEP_VALIDATE`·`PLAN_STEP_PLAN`·`PLAN_STEP_SHOW`를 함께 남긴다(경로 `terraform.<단계>`). 첫 이슈 코드는 기존과 같다(`PLAN_COMMAND_FAILED`) | `plan_failed` 이벤트의 issues는 코드 목록이라(49) 단계도 코드로 남겨야 보인다. 기존 코드를 바꾸지 않아 소비자 영향이 없다. 출력 원문은 여전히 버린다 |
| C7 `cluster_name` (B-12) | 연결 경로 Plan은 `cluster_name`이 `cmp-`로 시작해야 한다(`INVALID_CLUSTER_NAME`). 템플릿 `ResourcePrefix` 고정은 C15(v2)로 미룬다 | Terraform IAM 이름이 모두 `${name}-…`라 이름 접두어가 곧 IAM 접두어다. 같은 계정 경로는 템플릿을 쓰지 않으므로 검사하지 않는다(기존 입력 호환) |
| C8 플랫폼 주체 (B-02·B-16) | **23b 설계를 따른다**: 주체 = 데모 인스턴스 역할 그대로(`CONNECTION_PLATFORM_AWS_PROFILE=cmp-demo-build`, `CONNECTION_PLATFORM_PRINCIPAL_ARN`=인스턴스 역할 ARN). AssumeRole 권한은 23b가 `cmp-demo-build` 스택 `ConnectionAssumePolicy`(리소스 `role/*`, 태그 `cmp:managed-by` + ExternalId `cmp-*` + Null)로 반영했다. **B-16은 후속**: 주체를 이름 고정 역할로 바꾸면 이미 만든 사용자 연결 스택의 신뢰 정책이 깨지므로, 바꿀 때 기존 연결 마이그레이션(사용자 스택 매개변수 갱신 또는 재생성 안내)이 함께 필요하다 | 오케스트레이터 결정(2026-11-06): 사용자가 이미 이 ARN으로 스택을 만들 수 있다. **기각한 대안(이 PR 초안):** 별도 스택 `cmp-demo-connector`(이름 고정 역할 `cmp-platform-connector`가 `role/cmp-*`만 Assume, 인스턴스 역할은 그 역할만 Assume, 템플릿 버킷 포함, `deploy-connector-stack.sh`). B-16을 고치지만 23b와 주체·버킷이 중복되고 기존 연결을 끊는다 |
| C9 사용자 역할 이름 | 템플릿 역할 이름은 고정하지 않는다(23b 권한이 이름이 아니라 태그로 범위를 정함) | 초안의 `cmp-<종류>-<External ID hex>` 고정은 C8 초안의 `role/cmp-*` 범위용이라 함께 뺐다 |
| C10 템플릿 호스팅 (B-05) | 23b의 `cmp-demo-connection-templates`(버킷 `cmp-demo-714066696240-apne2-connection-templates`, `connections/v1/*` 공개, `deploy-connection-templates.sh`)를 쓴다. 이 티켓은 업로드하지 않는다 | 중복 제거 |
| C11 공유 프로필 파일 (B-03) | 호스트 `/opt/cmp/data/aws-connections/`(키 없음: ARN·External ID·프로필 이름). api: `CONNECTION_AWS_CONFIG_FILE=/run/cmp/aws-connections/config`(쓰기), `CONNECTION_AWS_BASE_CONFIG_FILE=/run/cmp/aws/config`. worker: `AWS_CONFIG_FILE=/run/cmp/aws-connections/config`(읽기 전용). `start.sh`가 `ensure-secrets.sh <env> <secrets> /opt/cmp/data/aws-connections`로 파일이 없을 때만 기본 파일로 씨앗을 둔다. API는 **시작 때** DB에서 파일을 다시 쓴다 | 디렉터리 마운트라 `os.replace` 원자 교체가 보인다. `/secrets/` 마운트는 모두 읽기 전용이라는 기존 규칙을 지키려고 data 아래에 둔다. 시작 재작성으로 재배포 뒤에도 연결 프로필이 남는다(N12) |
| C12 프로필 연쇄 | 생성 프로필 `cmp-conn-<hex>`의 `source_profile`은 `cmp-demo-build`(`credential_source = Ec2InstanceMetadata`, `role_arn` 없음) = 인스턴스 역할 = `CONNECTION_PLATFORM_PRINCIPAL_ARN`. ECR 토큰 발급은 새 설정 `PLATFORM_ECR_AWS_PROFILE`(데모 `cmp-demo-build`, 없으면 41처럼 연결 프로필)로 분리 | 사용자 신뢰 정책의 주체와 실제 AssumeRole 호출자가 같다. 토큰 발급과 연결 주체를 따로 바꿀 수 있다 |
| C13 응답 필드 | `POST /connections`와 `GET /connections/{id}` 응답에 `platform_principal_arn`(AWS 연결, 설정 없으면 `null`; GCP는 `null`) 추가. 다른 필드·코드는 그대로 | 사용자가 링크 없이 템플릿 파일로 만들 때도 매개변수 값을 알 수 있다(B-05). 추가 필드라 기존 웹 화면은 영향 없다. 표시는 웹 담당과 협의 |
| C14 GKE pull 갱신 | GKE의 ECR 토큰은 계속 worker 플랫폼 프로필(`PROVISIONING_AWS_PROFILE`)로 발급한다(대상 연결 아님) | 레지스트리는 플랫폼 계정 것이다. GCP 대상의 연결은 GCP 주체(48)라 AWS 연결 프로필이 없다. 이슈 문구("GKE pull 갱신에 실행별 전달")와 다르게 정한 이유다 |
| C15 템플릿 v2 (B-12·B-14, 후속) | 이 PR은 `infra/connections/*.yaml`을 **바꾸지 않는다**(23b가 올린 `connections/v1`과 같은 바이트). v2 후보: 대상 템플릿 `ResourcePrefix`를 `AllowedValues: [cmp-]`로 고정(B-12), `AuthorizeSecurityGroupEgress/Ingress`의 새 `security-group-rule` 리소스 허용 문장(`arn:${AWS::Partition}:ec2:*:${AWS::AccountId}:security-group-rule/*`, SG 자체는 계속 태그 조건), `eks:TagResource`를 생성 문장(`aws:RequestTag`)에 추가(B-14). Terraform은 바꾸지 않는다 | 저장소 템플릿을 바꾸면 `deploy-connection-templates.sh`가 같은 `v1`에 다른 내용이라 실패하고, 23b 버킷 정책은 `connections/${TemplateVersion}/*` 하나만 공개해 `v2`로 올리면 `v1` 링크가 닫힌다. 버전 전환 방식은 23b 담당과 정한다. SG 규칙에 Terraform 태그를 붙이면 기존 State 재Plan이 update가 되어 14 F9 복구가 거절된다. 실제 통과는 대상 역할 Apply(47)로만 확정된다 |

## 함수 경계 (47·48·53이 이어 쓸 곳)
- `cmp_providers.aws_credentials`: `connection_profile(connection_id)`, `connection_id_of(credential_ref)`, `resolve_aws_profiles(credential_ref, *, platform_profile) -> AwsProfiles`. 47(대상 권한·교차 계정 ECR)은 `AwsProfiles.provider`를 대상 주체로 쓴다.
- `cmp_api.connections.check_target_connection(session, connection_id, *, account_id) -> EnvironmentConnection`. GCP 연결 검사(48)는 같은 자리에 GCP 판을 둔다.
- `cmp_worker.activities.k8s_workloads.ApprovedWorkloads.runtime_for(approved)`: provider 훅. AWS는 `CliRuntime.with_profile`. GKE(48)는 같은 훅으로 gcloud configuration을 바꿀 수 있다.
- 템플릿: `infra/connections/*-role.yaml`(사용자, 23b 호스팅). 47이 대상 권한을 넓히면 C15와 함께 `v2`로 올린다.

## 수용 기준
- [ ] 연결 확인이 맞게 만든 사용자 역할로 성공한다. — 권한·링크는 23b가 반영. 사용자 계정 스택 생성 후 verify 확인 대기(승인 필요)
- [x] Plan·Apply가 선택한 연결의 역할로 실행된다(단위 테스트). — Plan·Apply·워크로드 runner가 받은 `AWS_PROFILE`·`--profile`·backend profile 확인. 실계정은 승인 후
- [ ] 사용자가 웹에서 스택 생성 링크와 플랫폼 주체 값을 받는다. — 링크 설정은 23b가 서버에 반영, 이 PR은 응답 `platform_principal_arn` 추가. 웹 표시는 협의 필요

## Test Seam
`tests/test_connection_credentials.py`
- 해석: `cmp-conn-<hex>`(44 형식)·`conn-<hex>` → 연결 프로필·State는 플랫폼, 일반 ID → 플랫폼 하나, 잘못된 형식 → `INVALID_CREDENTIAL_REF`, 기본 프로필 거절
- Plan: 가짜 `aws`·`terraform`이 받은 `--profile`·`AWS_PROFILE`·`-backend-config=profile=`, 연결 경로 `cluster_name` 접두어, D-20 코드·단계
- Apply: 승인된 spec의 연결로 `AWS_PROFILE`·`--profile`, backend profile
- 연결 검사: 없음·종류·미확인·계정 불일치 코드, `PlanPublisher` 실패 코드
- AWS 워크로드: 승인된 spec 산출물의 연결 → `with_profile` 런타임으로 계정 확인·접속
- API: 시작 시 생성 파일 재작성, 응답의 `platform_principal_arn`, ECR 발급 프로필 분리
- 데모: compose 공유 경로·주체 프로필, `ensure-secrets.sh` 씨앗(한 번만), 생성 프로필 → `cmp-demo-build`(인스턴스 역할) 연쇄

## 서버 반영 순서 (각 단계 사용자 승인 필요)
23b가 AWS 권한·템플릿 버킷·`.env`의 주체 ARN·템플릿 URL을 이미 반영했다. 이 PR은 AWS 변경이 없다.
1. `bash infra/demo/release.sh HEAD`(또는 Deploy demo) — `start.sh`가 `/opt/cmp/data/aws-connections/config`를 만들고, api가 시작 때 DB의 연결로 다시 쓰고, worker가 그 파일을 `AWS_CONFIG_FILE`로 읽는다. `.env` 변경 없음.
2. 서버 점검(README 「Provisioning의 연결 사용」): worker `grep -c '^\[profile cmp-conn-' "$AWS_CONFIG_FILE"`가 역할 등록 AWS 연결 수, api `sts get-caller-identity --profile cmp-demo-build`가 인스턴스 역할.
3. 테스트 계정에서 대상 연결 생성 → 링크로 스택 → 역할 등록 → verify `CONNECTED` → 그 연결로 Plan(승인된 테스트 범위·비용·정리 규칙에 따라). 대상 권한 부족은 47.
4. (결정 후) 템플릿 v2(C15) 업로드와 `CONNECTION_TEMPLATE_BASE_URL` 전환 — 23b 버킷 정책의 버전 공개 범위를 함께 정한다.

## 관련 문서
- [ADR-005](../../docs/decisions/ADR-005.md) · [23](23-environment-connections.md) · [23b](23b-connection-launch-stack-hotfix.md) · [28](28-build-registry-runtime.md) · [41](41-demo-ecr-pull-wiring.md) · [shared-contracts 환경 연결](../../specs/provisioning/shared-contracts.md#환경-연결-23) · [`infra/demo/README.md`](../../infra/demo/README.md)

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인 (실계정 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 — ADR-005 후속 메모, shared-contracts 환경 연결, plan.md, 23·28·41 후속 줄, README. OpenAPI는 추가 필드 두 개. DB 변경 없음
- [ ] 구현·계약 정합성 및 최종 리뷰 — 조정 세션 리뷰 대기
- [x] 미검증 항목과 결과 증거 기록

## 변경 요약
- `packages/providers/src/cmp_providers/aws_credentials.py`(신규), `aws_preview.py`(연결 해석·backend profile·`cluster_name`·단계 코드), `aws_apply.py`(승인된 spec의 연결·backend profile)
- `apps/api`: `connections.check_target_connection`·`credential_ref` 공용 이름, `main.py` 시작 시 생성 파일 재작성, 응답 `platform_principal_arn`, `platform_ecr_aws_profile`
- `apps/worker`: `PlanPublisher` 연결 검사, `ApprovedWorkloads.runtime_for`·`Approved.spec`, `AwsWorkloadsActivities.runtime_for`, `CliRuntime.with_profile`
- `infra/demo/compose.yaml`(공유 연결 파일·`PLATFORM_ECR_AWS_PROFILE`, 23b·44 값 유지)·`ensure-secrets.sh`(선택 3번째 인자로 씨앗)·`start.sh`·`README.md`. 템플릿·스택 파일 변경 없음
- 문서: 이 티켓, `tasks.md`, ADR-005, `shared-contracts.md`, `plan.md`, 23·28·41, `docs/contracts/openapi.yaml`
- 테스트: `tests/test_connection_credentials.py`(신규), `test_demo_runtime.py`·`test_plan_publish.py`(기대값 갱신), `test_aws_workloads.py`(`Plan(spec=)` 도우미)

## 검증 기록 (2026-11-06, origin/main `b65c6cd`(23b #208·44 #206) 병합 후)
| 명령·환경 | 결과 |
| --- | --- |
| 새 테스트 (구현 전) | 수집 실패 `ImportError: check_target_connection`·`cmp_providers.aws_credentials` 없음 (RED) |
| 병합 직후 44 테스트 | `test_aws_start_takes_the_connection_profile_and_account` 실패 → 44가 `credential_ref.id`에 `cmp-conn-<hex>`를 넣고 ID가 32 hex인 것을 반영(C1: 두 형식, 12~32 hex) |
| `make check` (ruff + pytest) | 2122 passed, 9 skipped |
| `make openapi` | origin/main 대비 `ConnectionDetail`·`CreateConnectionResponse`에 `platform_principal_arn`만 추가 |
| `alembic upgrade head` + `alembic check` (SQLite) | head `20261106_01`, `No new upgrade operations detected.` (DB 변경 없음) |
| `infra/connections/*.yaml` | origin/main(= 23b가 올린 `v1`)과 같은 바이트 |
| `bash -n` `ensure-secrets.sh`·`start.sh` | 통과 |
| 초안 단계: `docker compose config`, `uvx cfn-lint`(템플릿), botocore 연쇄(`cmp-conn-*` → `source_profile` 프로필의 `credential_source = Ec2InstanceMetadata`) | 공유 경로·마운트 확인, 오류 없음, IMDS까지 해석(서버 밖이라 IMDS만 실패). 병합 후 연쇄는 `test_generated_profiles_chain_to_the_instance_role_build_profile`로 고정 |

### 미검증·제한 사항
- 서버 릴리스 뒤 공유 파일 생성·api 시작 재작성·worker 해석, 실계정 연결 verify, 교차 계정 Plan/Apply는 하지 않았다(승인 필요).
- 템플릿 v2 후보(C15)는 이 PR에 없다. B-14는 대상 역할 실제 Apply(47)로만 확정된다.
- 분석 스캔(`get_session(credential_ref)`)이 worker의 새 `AWS_CONFIG_FILE`로 `cmp-conn-*`를 해석하는지는 경로상 같지만 분석 실행으로 확인하지 않았다(분석 담당 협의).
- AWS 읽기 전용 조회(`simulate-*`)는 SSO 토큰 만료로 하지 않았다. 23b가 권한을 시뮬레이션으로 확인했다(23b 티켓).
