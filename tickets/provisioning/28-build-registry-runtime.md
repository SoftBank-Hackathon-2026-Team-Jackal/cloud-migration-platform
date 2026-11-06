# Ticket 28 — 빌드·이미지 레지스트리 실행 환경 (버킷 권한·ECR·worker 빌드)

## 상태
IN_PROGRESS — [Issue #152](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/152). 로컬 구현·검증 완료. 새 스택 배포와 릴리스 반영(원격 변경, 사용자 승인 필요), 서버에서 실제 push 확인이 남았다.

## 목적
빌드 모듈(B-01)은 구현됐지만 데모 서버에서 실행할 환경이 없다. 샌드박스는 배포 버킷에 소스·테스트 묶음·빌드 로그를 쓸 수 없고, 플랫폼 빌드 결과를 담을 ECR 저장소와 ECR 권한도 없다. worker 컨테이너에는 Docker CLI와 호스트 Docker 연결이 없다. ADR-003의 "Build 실행 권한/Registry 접근은 B-01 착수 시 구체화"를 데모 환경 기준으로 정한다.

## 관련 요구사항
PROV-001, PROV-004, PROV-010, PROV-021

## 선행 작업
Blocked by: B-01 (DONE), 21 (로컬 완료). 29(#153, Agent 이미지 pull·토큰 발급)가 이 티켓의 저장소·권한 이름을 쓴다.

## 현재 상태 (2026-11-06, 계정 `714066696240`, ap-northeast-2)
- 인스턴스 역할 `cmp-demo-control-plane-InstanceRole-*`: State 버킷 `cmp/*` 읽기·쓰기, 배포 버킷 `releases/*` 읽기만. ECR 권한 없음.
- ECR: 원본 데모 앱용 `cmp-demo-api`·`cmp-demo-chat`만 있다. 플랫폼 저장소 없음.
- 인스턴스: IMDSv2 필수, hop limit 2(라이브 확인) → worker 컨테이너가 인스턴스 역할에 닿는다. x86_64.
- 호스트에는 Docker·buildx v0.37.2가 설치돼 있다(UserData). worker 이미지에는 Docker CLI가 없다.
- main의 `cloudformation.yaml`에는 서버에 반영되지 않은 21번 변경(EIP·443)이 있어, 그 스택을 갱신하면 공인 IP가 바뀐다.

## 작업 범위
- 포함: 새 스택 `cmp-demo-build`(`infra/demo/build-stack.yaml`)의 ECR 저장소와 기존 역할용 인라인 정책, 배포 스크립트(`deploy-build-stack.sh`), worker 이미지의 Docker CLI·buildx, compose의 소켓·`BUILD_*` 설정, 빌드 전용 AWS 프로필, 문서·테스트
- 제외: `cmp-demo-control-plane` 스택 갱신, 빌드 모듈 코드(`apps/`) 변경, 대상 클러스터 pull 권한(EKS 노드 역할·29의 GKE 토큰), Caddy·공개 경로(#154)

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 스택 분리 | 이 티켓의 AWS 리소스는 새 스택 `cmp-demo-build`에만 둔다. 기존 역할에는 `AWS::IAM::RolePolicy`(정책 이름 `cmp-demo-build`)로 인라인 정책을 붙이고, 역할 이름은 파라미터(배포 스크립트가 기존 스택의 `InstanceRole` 물리 ID를 읽기 전용으로 조회) | 기존 스택 갱신은 미반영 EIP를 함께 적용해 공인 IP를 바꾼다(Issue 결정). 관리형 정책 대신 인라인 정책이라 역할에 붙은 정책 수 제한과 무관 |
| D2 저장소 | `cmp-platform-builds`: `IMMUTABLE` 태그, 푸시 시 스캔, AES256, 태그 없는 이미지만 7일 뒤 만료, 스택 삭제·교체 시 보존(`Retain`) | 승인된 Plan이 Digest로 이미지를 고정하므로 태그 있는 이미지는 만료하지 않는다. 빌드 모듈이 `IMMUTABLE`이 아니면 거부한다 |
| D3 ECR 권한 | `ecr:GetAuthorizationToken`(리소스 `*`, 저장소 지정 불가)만 와일드카드. 그 저장소에 pull(`BatchGetImage`·`GetDownloadUrlForLayer`·`BatchCheckLayerAvailability`·`DescribeImages`·`DescribeRepositories`)과 push(`InitiateLayerUpload`·`UploadLayerPart`·`CompleteLayerUpload`·`PutImage`) | 빌드 모듈이 `describe-repositories`(불변 확인)·`describe-images`(재사용·Digest 대조)·로그인·push를 호출한다. 29의 토큰 발급도 같은 pull 권한을 쓴다 |
| D4 버킷 접두사 | 배포 버킷 `cmp-demo-714066696240-apne2-deploy`의 `sources/`(`package_source`), `tests/`(`upload_suite`), `builds/`(빌드 로그 키)에 `GetObject`·`PutObject`. `releases/`는 손대지 않는다(템플릿 Rule로 접두사가 `releases`가 되지 못하게 함). 접두사는 파라미터 | Issue 예시(`suites/`, `build-logs/`)가 아니라 **현재 코드가 실제로 쓰는 키**에 맞췄다. 박승철님 확인 필요 — 바꾸면 코드 기본값과 파라미터를 함께 바꾼다. 삭제 권한 없음 |
| D5 ListBucket | 배포 버킷에 `s3:ListBucket`을 접두사 조건 **없이** 준다 | `AwsCliArchiveStore.exists`는 `head-object`의 404로 "없음"을 판단한다. S3는 ListBucket 권한이 있어야 404를 주고(없으면 403), 이 판단에는 `s3:prefix`가 없어 접두사 조건이 거짓이 된다 → 첫 업로드가 항상 `STORE_ERROR`. 목록에 보이는 것은 키 이름(커밋 SHA 릴리스 이름)뿐이다. 실제 AWS에서 미검증 |
| D6 worker 빌드 | 호스트 Docker 소켓(`/var/run/docker.sock`)을 **worker에만** 마운트. worker 이미지에 Docker CLI 28.5.2(정적 바이너리의 `docker`만)·buildx v0.37.2(호스트와 같은 버전), amd64·arm64 SHA-256 고정. 빌드는 호스트 데몬의 기본 `docker` 드라이버로 돌고, 빌드 중에만 빌드 컨테이너가 생긴다 | 별도 BuildKit 컨테이너가 필요 없다. 기본 드라이버의 `--metadata-file`의 `containerimage.digest`가 push된 manifest Digest와 같은지 classic overlay2 데몬(docker:28-dind)과 로컬 레지스트리로 확인했다(빌드 모듈은 ECR Digest와 다시 대조) |
| D7 소켓 위험 | (후속: 빌드 `RUN`의 인스턴스 메타데이터 접근은 [54](54-build-isolation.md)에서 호스트 규칙으로 차단) 소켓을 가진 컨테이너는 호스트 root와 같다(임의 컨테이너 실행·호스트 마운트). 데모 단일 호스트에서만 허용하고, worker는 이미 인스턴스 역할·GCP 키를 가진 신뢰 경계다. 빌드 대상 소스는 업로드된 앱 코드이므로 Dockerfile `RUN`이 호스트 데몬에서 돈다는 점도 같은 위험이다 | 운영급 대안: rootless BuildKit(`moby/buildkit:rootless`)을 별도 서비스로 두고 worker는 `BUILDKIT_HOST`(TLS)로만 접속하거나, 빌드를 CodeBuild·Kaniko 같은 격리 실행으로 옮긴다. 빌드 모듈은 `docker buildx`만 호출하므로 원격 builder로 바꿔도 코드 영향이 작다 |
| D8 AWS 프로필 | `ensure-secrets.sh`가 `[profile cmp-demo-build]`(인스턴스 역할, `role_arn` 없음)을 추가로 만든다. worker에 `BUILD_AWS_PROFILE=cmp-demo-build`와 `AWS_PROFILE=cmp-demo-build` | 빌드 모듈의 `aws` 호출(`ImageBuilder`)은 프로필 인자를 받지 않고 프로세스 환경을 쓴다. 기본 프로필 대신 이름 붙은 프로필로 신원·리전을 명시한다. Provisioning은 `PROVISIONING_AWS_PROFILE`을 하위 프로세스에 직접 넣으므로 영향이 없다. `PROVISIONING_AWS_ROLE_ARN`을 써도 빌드는 인스턴스 역할로 한다 |
| D9 설정 주입 | `BUILD_ECR_REPOSITORY`·`BUILD_SOURCE_BUCKET`·`BUILD_LOG_BUCKET`·`BUILD_PLATFORM`은 서버 `/opt/cmp/.env`에서 읽고(compose 기본값 빈 문자열), `deploy-build-stack.sh`가 스택 출력으로 넣을 줄을 출력한다. 넷 다 비면 사전 빌드 이미지 경로를 유지 | 인스턴스 역할에는 `cloudformation:DescribeStacks`가 없어 서버가 스택 출력을 직접 읽지 못한다. 스택 배포 전에 빌드를 켜지 않도록 운영자가 명시적으로 켠다(opt-in) |

## 수용 기준
- [ ] 데모 서버의 worker가 빌드 모듈로 플랫폼 ECR에 push하고 Digest를 돌려준다. — **대기**: 스택 배포·릴리스 승인 필요. 로컬: 이미지에 CLI·buildx, 기본 드라이버 Digest 일치 확인
- [ ] 샌드박스가 정한 접두사에 소스·테스트 묶음·로그를 읽고 쓸 수 있고, `releases/`는 그대로다. — 템플릿·테스트로 범위 확인, 실제 권한은 서버 반영 후
- [x] 자격 증명이 이미지·저장소·로그·Temporal 이력에 없다. — 프로필은 `credential_source`만, 이름만 환경변수로, 빌드 모듈은 임시 `DOCKER_CONFIG`에 로그인(기존 B-01)
- [x] 템플릿 구조 테스트(권한 범위, 태그 불변)와 worker 이미지 빌드 테스트가 있다.

## Test Seam
`tests/test_build_registry_runtime.py`(템플릿 리소스·권한·접두사와 코드 기본값 대조, 기존 스택 불변, 소켓·설정·프로필), `tests/test_demo_runtime.py`(CLI 고정 7종), worker 이미지 로컬 빌드, 서버에서 실제 빌드 스모크.

## 서버 반영 순서 (각 단계 사용자 승인 필요)
1. `bash infra/demo/deploy-build-stack.sh --plan` → 변경 세트 검토(저장소 1, 인라인 정책 1)
2. `bash infra/demo/deploy-build-stack.sh` → 출력된 `BUILD_*` 네 줄을 SSM 세션에서 `/opt/cmp/.env`에 추가
3. `bash infra/demo/release.sh HEAD` → worker 교체, `start.sh`가 `docker version`·`docker buildx version`까지 확인
4. 서버 스모크(README 「빌드 실행 환경」)

## 관련 문서
- [ADR-003](../../docs/decisions/ADR-003.md) · [B-01](b-01-image-build.md) · [21](21-demo-provisioning-runtime.md) · [43](43-worker-image-packaging.md)(후속: worker 이미지 리소스 경로·Linux lock) · [`infra/demo/README.md`](../../infra/demo/README.md)

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인 (서버 검증 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 — 외부 API·도메인 경계 변경 없음. OpenAPI 변경 없음. ADR-003의 미결 항목(실행 권한·Registry 접근)을 이 티켓 D2–D8로 구체화
- [ ] 구현·계약 정합성 및 최종 리뷰 — 조정 세션 리뷰 대기
- [x] 미검증 항목과 결과 증거 기록

## 변경 요약
- `infra/demo/build-stack.yaml`(신규): ECR `cmp-platform-builds`, 기존 역할 인라인 정책 `cmp-demo-build`
- `infra/demo/deploy-build-stack.sh`(신규): 계정 확인, 기존 스택 읽기 전용 조회, `--plan`(변경 세트만), 배포 후 `.env` 줄 출력
- `infra/demo/Dockerfile`: worker에 Docker CLI·buildx(체크섬 고정)
- `infra/demo/compose.yaml`: worker에만 Docker 소켓, `BUILD_*`·`AWS_PROFILE`
- `infra/demo/ensure-secrets.sh`: `cmp-demo-build` 프로필
- `infra/demo/start.sh`: 상태 검사에 `docker version`·`docker buildx version`
- `tests/`: 위 Test Seam. `apps/`·`packages/`·`cloudformation.yaml` 변경 없음

## 검증 기록 (2026-11-06)
| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_build_registry_runtime.py tests/test_demo_runtime.py` | 23 passed |
| `make check` (ruff + pytest) | 1748 passed, 8 skipped |
| `docker build --platform linux/amd64 --target worker` (Docker 28.4, 에뮬레이션) | 성공. 다운로드 7건 모두 `sha256sum -c` OK. Docker 28.5.2, buildx v0.37.2, Terraform 1.17.0, aws-cli 2.35.21 |
| 그 이미지 + 호스트 소켓 마운트, 새 임시 `DOCKER_CONFIG`로 `docker buildx build --push --metadata-file` → 로컬 `registry:2` | 데몬 연결(API 1.51) 성공, `containerimage.digest` = 레지스트리 `Docker-Content-Digest` |
| `aws cloudformation validate-template`(읽기 전용, `cmp-aws-prod`) | 통과 |
| `docker compose -f infra/demo/compose.yaml config`(가짜 값) | worker에만 소켓, `BUILD_*` 빈 값 기본 |
| Digest 확인: `docker:28-dind`(overlay2, containerd 스냅샷 아님) + `registry:2`, `docker buildx build --push --metadata-file` | `containerimage.digest` = 레지스트리 `Docker-Content-Digest` |
| 라이브 읽기: `ec2 describe-instances` | hop limit 2, IMDSv2 필수, x86_64 |

### 미검증·제한 사항
- 새 스택 배포, 릴리스, 서버에서 실제 ECR push·S3 쓰기는 실행하지 않았다(승인 필요).
- D5(접두사 조건 없는 ListBucket의 필요성)는 AWS 문서 동작에 근거하며 실제 계정에서 확인하지 않았다.
- 호스트 Docker Engine 버전(AL2023 패키지)을 확인하지 않았다. CLI 28.5.2는 구 API로 협상하지만 서버에서 `docker version`으로 확인한다(start.sh 검사).
- 대상 클러스터의 pull: EKS 노드 역할의 ECR 읽기 권한과 GKE용 토큰(29)은 이 티켓 범위 밖이다. 29는 `PROVISIONING_AWS_ROLE_ARN`을 쓰는 경우를 대비해 `cmp-demo-build` 프로필(인스턴스 역할)로 토큰을 발급해야 이 정책이 적용된다.
- 접두사 `sources/`·`tests/`·`builds/`는 박승철님 확인 대기.

## 후속 (티켓 46, 2026-11-06)
- 46은 AWS 리소스를 추가하지 않는다. 연결 AssumeRole 권한은 23b가 이 스택(`cmp-demo-build`)의 `ConnectionAssumePolicy`로 넣었다. [46](46-connection-credentials.md)
