# Ticket 41 — 데모 서버 API에 플랫폼 ECR pull 설정 전달 (29 후속)

## 상태
IN_PROGRESS — [Issue #176](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/176). 로컬 구현·검증 완료. 리뷰, 서버 `.env` 반영과 릴리스(원격 변경, 사용자 승인 필요), 서버에서 실제 토큰 발급 확인이 남았다.

## 목적
29는 플랫폼이 자기 AWS 주체로 단기 ECR 토큰을 발급하고, K3s Agent가 `GET /agents/{agent_id}/jobs/{job_id}/image-pull`로 받아 pull Secret을 만든다. 28로 스택 `cmp-demo-build`(ECR `cmp-platform-builds`, 인스턴스 역할 인라인 정책: `ecr:GetAuthorizationToken`·그 저장소 pull·push)를 2026-11-06 배포했다. 그런데 데모 `infra/demo/compose.yaml`의 **api** 서비스는 `PLATFORM_ECR_REGISTRY`·`PLATFORM_ECR_REPOSITORIES`·`CONNECTION_PLATFORM_AWS_PROFILE`과 AWS 설정 파일을 받지 못한다. 그래서 데모 서버에서는 ON_PREM rollout 작업에 플랫폼 레지스트리가 빠지고(`onprem_rollout.configured_platform_registry` → `None`), ECR 이미지가 든 승인은 `IMAGE_REGISTRY_NOT_ALLOWED`가 되며, Agent용 토큰 발급도 쓸 수 없다. 이 티켓은 그 설정 전달만 메운다.

## 관련 요구사항
PROV-009, PROV-010, PROV-021

## 선행 작업
Blocked by: 28 (#152, 스택 배포 완료), 29 (#153, main 병합). 코드(`apps/`·`packages/`) 변경 없음.

## 작업 범위
- 포함: compose `api`의 AWS 설정 파일 읽기 전용 마운트·`AWS_CONFIG_FILE`, `CONNECTION_PLATFORM_AWS_PROFILE=cmp-demo-build`, `PLATFORM_ECR_REGISTRY`(기본 빈 값)·`PLATFORM_ECR_REPOSITORIES`(기본 `cmp-platform-builds`). `deploy-build-stack.sh`가 `.env`에 넣을 `PLATFORM_ECR_REGISTRY` 줄 출력. README 설정 표·반영 순서·점검 명령, 테스트.
- 제외: worker GKE pull 경로 변경(확인만, 아래 D4), 서버 `.env` 수정·릴리스·AWS 변경, Agent·웹 저장소, 다른 담당 범위.

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 api 자격 증명 | api에 `/opt/cmp/secrets/aws:/run/cmp/aws:ro`와 `AWS_CONFIG_FILE=/run/cmp/aws/config`만 준다. 정적 키 없음. **GCP 키 마운트·gcloud 구성은 주지 않는다** | 토큰 발급(`boto3_minter`)과 환경 연결(`AwsConnector`)은 `boto3.Session(profile_name=...)`을 쓴다. botocore는 이름 붙은 프로필이 설정 파일에 없으면 `ProfileNotFound`로 실패하므로 설정 파일이 필요하다(로컬 botocore 1.43.124로 확인). api는 GCP를 직접 부르지 않는다(GCP 연결 검사 `verify_adc_principal`은 api에 ADC가 없으면 실패하는 기존 동작 그대로) |
| D2 플랫폼 프로필 | `CONNECTION_PLATFORM_AWS_PROFILE=cmp-demo-build`. `ensure-secrets.sh`가 이미 이 프로필을 인스턴스 역할 자체(`credential_source = Ec2InstanceMetadata`, `role_arn` 없음)로 만든다. `cmp-demo-provisioning`은 쓰지 않는다 | `PROVISIONING_AWS_ROLE_ARN`을 쓰면 Provisioning 프로필은 다른 역할을 Assume하므로 28 인라인 정책(인스턴스 역할에 붙음)이 적용되지 않는다. worker의 빌드 프로필과 같은 이름이라 ECR 권한 주체가 하나다 |
| D3 연결 관리 영향 | 환경 연결(`routes/connections.py`의 `AwsConnector`, `sync_aws_config`)도 이 프로필을 쓰게 되지만 **동작은 같다**. 확인: (1) botocore에서 프로필을 명시하면 자격 체인에서 `env` 공급자만 빠지고 마지막 `iam-role`(IMDS)은 그대로다 — 프로필에 키·`role_arn`이 없으므로 결국 같은 인스턴스 역할로 해석된다. api 컨테이너에는 AWS 키 환경변수도 `~/.aws`도 없어 지금도 기본 체인이 IMDS로 간다. (2) `sts:AssumeRole`은 리전을 명시해 부르므로 프로필의 `region`이 바뀌어도 무관하다. (3) `sync_aws_config`는 `CONNECTION_AWS_CONFIG_FILE`이 비어 있으면(데모 compose에 없음) 아무것도 하지 않는다. 고객 역할 신뢰 정책의 주체(인스턴스 역할 ARN)도 같다 | 프로필 없이 기본 체인을 두고 토큰 발급만 다른 설정을 쓰게 하려면 코드 변경이 필요하다. 이름 붙은 프로필을 명시하는 AGENTS.md 원칙과도 맞다 |
| D4 GKE 경로(확인만) | worker GKE의 `ecr_password`는 `GcpCliRuntime(aws_profile=PROVISIONING_AWS_PROFILE)`이 하위 프로세스의 `AWS_PROFILE`을 **`cmp-demo-provisioning`으로 덮어써** 실행한다(Issue 본문의 "`AWS_PROFILE=cmp-demo-build`로 이미 쓴다"와 다름). 서버 `.env`에 `PROVISIONING_AWS_ROLE_ARN`이 없으면 이 프로필도 인스턴스 역할이라 28 정책으로 동작한다. 있으면 Assume한 역할에 ECR 토큰·pull 권한이 있어야 한다. 이 티켓에서는 고치지 않는다 | 범위가 "확인만"이다. 28 미검증 항목에도 같은 지적이 있다. 후속 후보: GKE 토큰 발급을 빌드 프로필로 분리(worker 코드 변경) |
| D5 레지스트리 설정 | `PLATFORM_ECR_REGISTRY: ${PLATFORM_ECR_REGISTRY:-}`, `PLATFORM_ECR_REPOSITORIES: ${PLATFORM_ECR_REPOSITORIES:-cmp-platform-builds}`. 빈 값이면 `PlatformRegistry.parse`가 `None`을 돌려 29 이전과 같다(공개 레지스트리 이미지는 그대로, ECR 이미지는 `IMAGE_REGISTRY_NOT_ALLOWED`) | 28 D9와 같은 opt-in. 인스턴스 역할에는 `cloudformation:DescribeStacks`가 없어 서버가 스택 출력을 직접 읽지 못한다 |
| D6 스크립트 출력 | `deploy-build-stack.sh` apply 모드가 저장소 URI의 호스트(`${uri%%/*}`)를 `PLATFORM_ECR_REGISTRY=` 줄로 출력한다. 호스트가 `<account>.dkr.ecr.<region>.amazonaws.com`(스크립트의 고정 계정·리전)과 다르면 아무것도 출력하지 않고 실패한다 | 운영자가 값을 손으로 만들지 않게 한다. 다른 계정·리전 값이 `.env`에 들어가는 것을 막는다 |

## 수용 기준
- [x] api가 인스턴스 역할 프로필로 ECR 토큰을 발급할 수 있는 설정을 받는다(정적 키 없음, 설정 파일은 읽기 전용, GCP 마운트 없음). — compose·테스트로 고정. 실제 발급은 서버 반영 후
- [x] `PLATFORM_ECR_REGISTRY`가 비면 지금처럼 동작한다. — 렌더 결과 빈 값, `configured_platform_registry()` → `None` 확인
- [x] 서버 반영 뒤 `/opt/cmp/.env`에 넣을 값이 스크립트 출력과 README에 있다.
- [ ] 데모 서버에서 Agent가 `image-pull`로 토큰을 받는다. — **대기**: 리뷰·서버 반영 승인 필요

## Test Seam
`tests/test_demo_runtime.py`
- `test_api_uses_the_instance_role_build_profile_without_gcp_credentials`: api 설정 파일·`:ro` 마운트, 프로필이 worker 빌드 프로필과 같음, GCP 마운트·변수 없음
- `test_api_platform_registry_settings_default_to_disabled`: 기본값과 빈 레지스트리 → `None`
- `test_build_stack_apply_prints_the_platform_registry_from_the_repository_uri`, `test_build_stack_apply_refuses_a_repository_outside_the_expected_registry`: 가짜 `aws`·`git`을 `PATH`에 두고 `deploy-build-stack.sh` apply 모드를 실제로 실행
- 기존 `test_cloud_credentials_are_mounted_read_only_not_set_as_values`(정적 키 금지·secrets `:ro`)가 api에도 적용된다

## 서버 반영 순서 (각 단계 사용자 승인 필요)
1. `bash infra/demo/deploy-build-stack.sh` — 스택은 이미 있으므로 변경 없이 `.env` 줄만 다시 출력한다(`--no-fail-on-empty-changeset`). 또는 값 `PLATFORM_ECR_REGISTRY=714066696240.dkr.ecr.ap-northeast-2.amazonaws.com`을 그대로 쓴다.
2. SSM 세션에서 그 줄을 `/opt/cmp/.env`에 추가.
3. `bash infra/demo/release.sh HEAD` — api가 새 설정·마운트로 다시 만들어진다.
4. 점검(README 「빌드 실행 환경」의 api 명령): api에서 `cmp-demo-build` 프로필의 `sts get-caller-identity`가 인스턴스 역할, `configured_platform_registry()`가 그 레지스트리.
5. Agent v0.3.0으로 ECR 이미지를 쓰는 ON_PREM 실행에서 `cmp-ecr-pull` 생성 확인.

## 관련 문서
- [28](28-build-registry-runtime.md) · [29](29-image-pull-strategy.md) · [plan.md 29](../../specs/provisioning/plan.md) · [`infra/demo/README.md`](../../infra/demo/README.md)

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인 (서버 검증 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 — 외부 API·도메인 경계·DB 변경 없음. OpenAPI 재생성 차이 없음. plan.md 29 문단에 데모 설정 한 문장 추가
- [ ] 구현·계약 정합성 및 최종 리뷰 — 조정 세션 리뷰 대기
- [x] 미검증 항목과 결과 증거 기록

## 변경 요약
- `infra/demo/compose.yaml`: api에 `AWS_CONFIG_FILE`·`CONNECTION_PLATFORM_AWS_PROFILE`·`PLATFORM_ECR_*`, AWS 설정 읽기 전용 마운트
- `infra/demo/deploy-build-stack.sh`: `PLATFORM_ECR_REGISTRY` 줄 출력과 호스트 검사
- `infra/demo/README.md`: 설정 표(api 두 행, 빌드 표 api 행), 반영 순서(다섯 줄), api 점검 명령
- `specs/provisioning/tasks.md`(41 행·선행 관계), `specs/provisioning/plan.md`(29 문단), 이 문서
- `tests/test_demo_runtime.py`: 위 Test Seam 4개

## 검증 기록 (2026-11-06)
| 명령·환경 | 결과 |
| --- | --- |
| 새 테스트 4개 (구현 전) | 4 failed (RED) |
| `uv run pytest tests/test_demo_runtime.py tests/test_build_registry_runtime.py` | 29 passed |
| `make check` (ruff + pytest) | 1852 passed, 8 skipped |
| `make openapi` | `docs/contracts/openapi.yaml` 차이 없음 |
| `alembic upgrade head` + `alembic check` (SQLite) | `No new upgrade operations detected.` (DB 변경 없음) |
| `docker compose -f infra/demo/compose.yaml config`(가짜 비밀값) | api: `AWS_CONFIG_FILE=/run/cmp/aws/config`, 프로필 `cmp-demo-build`, `PLATFORM_ECR_REGISTRY=''`, 저장소 `cmp-platform-builds`, `/opt/cmp/secrets/aws`→`/run/cmp/aws` `read_only`. `PLATFORM_ECR_REGISTRY`를 주면 그 값 |
| botocore 1.43.124, 프로필만 있는 설정 파일 | 프로필 명시 시 공급자 체인 = 기본 체인에서 `env`만 빠짐(`iam-role` 유지), 리전 `ap-northeast-2`, 없는 프로필은 `ProfileNotFound` |
| api 설정 로드(`PLATFORM_ECR_REGISTRY=`) | `platform_ecr_registry=''` → `configured_platform_registry()` = `None`, `connection_aws_config_file=None` |

### 미검증·제한 사항
- 서버 `.env` 반영·릴리스, 서버 api에서 인스턴스 역할로의 실제 `ecr:GetAuthorizationToken`, Agent의 실제 토큰 수신·pull은 실행하지 않았다(승인 필요).
- 서버 `/opt/cmp/.env`의 `PROVISIONING_AWS_ROLE_ARN` 설정 여부를 확인하지 않았다 → D4의 GKE 토큰 발급 주체가 인스턴스 역할인지 미확인.
- 연결 관리의 실제 AssumeRole 동작이 같은지(D3)는 코드·botocore 체인으로만 확인했다.

## 후속 (티켓 46, 2026-11-06)
- D2·D3의 "토큰 발급과 연결이 같은 프로필"을 나눴다. api `PLATFORM_ECR_AWS_PROFILE=cmp-demo-build`(토큰 발급, 그대로 인스턴스 역할), `CONNECTION_PLATFORM_AWS_PROFILE`은 연결 주체 스택 배포 뒤 `cmp-platform-connector`. 설정이 없으면 41 동작 그대로. [46](46-connection-credentials.md)
