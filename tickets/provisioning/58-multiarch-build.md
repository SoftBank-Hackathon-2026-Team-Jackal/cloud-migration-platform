# Ticket 58 — 멀티 아키텍처(amd64·arm64) 이미지 빌드

## 상태
IN_PROGRESS (로컬 완료, 리뷰·서버 반영 승인 대기) — [Issue #278](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/278). 2026-11-07 데모 실행에서 발견했다. B-01·28·54 후속.

번호 메모: Issue #229(닫힘, "[Provisioning 58] 실제 모드 분석 이벤트 발행")도 58을 썼지만 티켓 파일이 없다. 이 파일이 `58-*` 파일 번호를 쓴다.

## 배경 (2026-11-07 실측)
- 데모 서버 설정이 `BUILD_PLATFORM=linux/amd64`라 빌드 이미지가 단일 아키텍처였다(ECR `cmp-platform-builds`, `sha256:9d7a61f7…`).
- 사용자의 로컬 대상은 Apple Silicon 맥의 k3d(K3s)이고 노드는 `arm64`다.
- Plan이 앱·DB에 `kubernetes.io/arch In [amd64]` 노드 조건을 붙여 `chat-db-0`이 `FailedScheduling`(node affinity)으로 Pending됐고, 결과는 `WORKLOAD_NOT_READY`였다.
- arm64 하나로만 빌드할 수도 없다. 샌드박스 클러스터 `k3d-cmp-sandbox`가 데모 EC2(x86_64) 안에 있다. 두 아키텍처를 담은 매니페스트 목록이 필요하다.
- 데모 EC2 빌더는 지금 `linux/amd64`(+386)만 지원한다. arm64 binfmt(QEMU)가 없다(`docker buildx ls`).

## 관련 요구사항
PROV-001, PROV-004, PROV-021 · [ADR-003](../../docs/decisions/ADR-003.md)(같은 Digest를 Sandbox·배포에 쓴다)

## 선행 작업
Blocked by: B-01(완료), 28(로컬 완료), 54(로컬 완료). 54의 빌드 격리(`docker0`·IMDS 차단)를 그대로 둔다.

## 작업 범위
- 포함
  - `BUILD_PLATFORM` 쉼표 목록. 하나만 주면 지금과 같다. 허용 값 `linux/amd64`·`linux/arm64`, 그 밖은 `BUILD_PLATFORM_INVALID`.
  - Build 모듈: 플랫폼별 빌드 → 매니페스트 목록 push → 목록 Digest와 `platforms`를 결과에 담는다.
  - 빌드 결과 → `BuildAttempt` → Planning(`BuildEvidenceV2`·`ValidatedImageV2`) → Plan(`image.platforms`)까지 목록 전달.
  - 데모 서버 준비 스크립트: binfmt 설치·재부팅 유지(`infra/demo/build-binfmt.sh`), `start.sh` 설치, `runtime-check.sh` 빌더 플랫폼 확인, 안내 기본값.
- 제외: 노드 아키텍처 사전 대조 전체(#155), 서버 반영(오케스트레이터가 승인 후 수행), AWS 변경.

## 착수 결정 (2026-11-07)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 빌드 방식 | **플랫폼마다 한 번씩** 데몬 내장 builder(`docker` 드라이버)로 빌드·push하고, `docker buildx imagetools create`로 매니페스트 목록을 만든다. 아키텍처 이미지 태그는 `<태그>-amd64`·`<태그>-arm64`, 목록은 `<태그>` | 데모 서버 Docker(AL2023)는 classic 이미지 저장소라 `docker` 드라이버가 `--platform a,b` 한 번의 빌드를 거절한다(“Multi-platform build is not supported for the docker driver”). 이 방식은 54의 `docker0`·`BUILDX_BUILDER` 제거·entitlement 없음을 그대로 지킨다 |
| D1-a 기각: `docker-container` builder | `docker buildx create --driver docker-container` 후 `--platform a,b` | 54 D5(항상 데몬 자신의 builder)를 바꿔야 하고, builder 상태가 빌드마다 새로 만드는 `DOCKER_CONFIG` 밖에 있어야 하며, builder 수명·캐시를 따로 관리해야 한다 |
| D1-b 기각: containerd 이미지 저장소 켜기 | `daemon.json`의 `containerd-snapshotter` | Docker 재시작이 필요하고 기존 이미지·컨테이너(k3d 샌드박스, compose 스택)가 새 저장소에서 보이지 않게 된다 |
| D2 목록 Digest | 목록 Digest는 buildx metadata가 아니라 ECR이 그 태그에 기록한 값(`describe-images`)이다. 그 Digest로 `imagetools inspect --raw`를 읽어 `os/architecture` 집합이 요청한 플랫폼과 같은지 확인한다(attestation `unknown/unknown` 제외). 다르면 `BUILD_INDEX_PLATFORM_MISMATCH`, 읽을 수 없으면 `BUILD_INDEX_INVALID`(둘 다 `BUILD` 분류) | `imagetools create`는 metadata 파일을 쓰지 않는다. 아키텍처 이미지는 지금처럼 metadata Digest = ECR Digest(`ECR_DIGEST_MISMATCH`)를 확인한다 |
| D3 재시도 | 목록 태그가 이미 있으면 재빌드하지 않는다(기존). 아키텍처 태그가 이미 있으면 그 이미지를 재사용한다 | ECR 태그가 IMMUTABLE이라 같은 태그로 다시 push할 수 없다. 목록 단계에서 실패한 재시도가 이미 push한 이미지를 다시 쓴다 |
| D4 태그 정체성 | 태그 해시 입력의 플랫폼 칸은 `",".join(platforms)`. 하나면 이전과 같은 문자열이라 기존 태그·재사용이 그대로다 | 목록이 다르면 다른 이미지다(Build store 키도 같은 문자열) |
| D5 필드 | Build 모듈·연결 모델(`BuildContext`·`BuildOutcome`·`BuildTarget`·`BuildResult`·`BuildSettings`)은 `platforms: list` 하나를 갖고 `platform`은 첫 항목을 돌려주는 읽기 속성이다. 생성 때 `platform=`(단일 값)도 받아 한 항목 목록으로 읽는다 | 기존 호출·테스트가 그대로 동작한다. 이 모델들은 메모리 안에서만 쓰인다(Temporal 이력·저장소에 없다) |
| D6 저장 계약 | `BuildAttempt`(빌드 시도 아티팩트로 저장)는 `platform`을 남기고 `platforms`를 더한다. `platform`은 **Sandbox가 실행한 플랫폼**(설정의 첫 항목)이고 `platforms`에 들어 있어야 한다. 58 이전 기록(`platforms` 없음)은 `[platform]`으로 읽는다. `ValidatedImageV2`도 같은 방식으로 `platforms`를 더한다(`platform`은 Sandbox 검증 플랫폼) | Planning 인계는 저장된 아티팩트를 다시 읽는다. 배포 중 이미 저장된 기록이 깨지지 않아야 한다 |
| D7 Sandbox 플랫폼 | `BUILD_PLATFORM`의 **첫 항목이 Sandbox 아키텍처**다(데모: `linux/amd64,linux/arm64`). 인계의 `validated_workloads[].platform`은 이 값이고 `PLATFORM_MISMATCH`는 그대로 목록 포함 여부로 판정한다. `runtime-check.sh`가 첫 항목과 호스트 아키텍처가 같은지 보여 준다 | 샌드박스 노드 아키텍처를 실제로 조회하지 않는다(새 설정을 늘리지 않음). Sandbox Helm은 목록 전체를 노드 조건으로 받아 amd64 노드에 그대로 배치된다 |
| D8 Plan | Plan의 앱·DB `image.platforms`는 **빌드 이미지의 목록 전체**다. DB도 같은 목록을 쓴다 | 고정한 공식 `postgres@sha256:5db6dac2…`는 amd64·arm64를 포함한 목록 이미지다(`imagetools inspect`로 확인). 앱과 같은 노드에 놓일 수 있어야 한다 |
| D9 binfmt | `infra/demo/build-binfmt.sh install`: 스크립트를 `/usr/local/sbin/cmp-build-binfmt`로 복사하고 `cmp-build-binfmt.service`(oneshot, `After`·`BindsTo`·`WantedBy=docker.service`)가 `tonistiigi/binfmt:qemu-v9.2.2`(digest 고정)를 `--privileged --network none`으로 실행해 다른 아키텍처(x86_64 호스트면 arm64)를 등록한다. `start.sh`가 IMDS 가드 다음, worker 시작 전에 실행한다. `BUILD_PLATFORM`에 arm64가 있으면 실패가 릴리스 실패다 | binfmt 등록은 재부팅 때 사라진다. 54와 같은 systemd 방식이다. 에뮬레이션된 `RUN`도 같은 빌드 컨테이너·`docker0`에 있어 IMDS 규칙이 그대로 걸린다. 설치 컨테이너는 네트워크가 없다 |
| D10 검증 seam | 단위 테스트: 설정 파싱, 빌드 명령 순서·태그·목록 Digest·플랫폼 불일치·재시도 재사용, 계약 호환, Planning·Plan 목록, `validate_target`. 구조 테스트: binfmt 스크립트·`start.sh` 순서·`runtime-check.sh`. 실제 빌드·스케줄은 로컬 Docker·k3d로 확인 | `make check`에 Docker를 넣지 않는다 |

## 수용 기준
- [x] `BUILD_PLATFORM=linux/amd64,linux/arm64`이면 빌드 결과 이미지가 두 플랫폼을 담은 매니페스트 목록 Digest다. 하나만 주면 이전과 같다.
- [x] 허용 밖 값은 `BUILD_PLATFORM_INVALID`(preflight `build` FAIL, runtime_report `error:BUILD_PLATFORM_INVALID`).
- [x] 빌드 결과 → `BuildAttempt` → Planning → Plan의 앱·DB `image.platforms`가 목록 그대로다.
- [x] arm64 노드에 두 플랫폼 목록으로 앱·DB가 배치된다(로컬 k3d). amd64만이면 이전과 같이 node affinity로 Pending이다.
- [x] Sandbox(amd64)는 목록 이미지에서 amd64를 받아 그대로 동작한다(노드 조건이 목록 전체를 허용).
- [ ] 데모 서버: binfmt 설치·`builder_arm64=yes`·실제 ECR 목록 push — 서버 반영 승인 대기.

## C-03(#155)과의 관계
이 변경은 C-03 중 “**빌드 이미지가 대상 노드 아키텍처를 지원하지 않아** Pod가 배치되지 않는” 경우를, 대상이 amd64·arm64 중 하나이면 없앤다(두 플랫폼을 모두 빌드하므로). 남는 범위(#155): 적용 전 대상 노드 아키텍처 사전 대조(`linux/arm/v7` 같은 다른 아키텍처·혼합 노드), 사용자가 고른 DB 이미지의 플랫폼 대조, 실패 Revision 동안의 DB 중단(51 R7).

## Test Seam
- `tests/test_multiarch_build.py`: 위 D10 단위·구조 테스트.
- 기존 `tests/test_image_build.py`(단일 플랫폼 경로·54 격리), `tests/sandbox/test_build*.py`, `tests/sandbox/test_handoff.py`, `tests/test_provisioning_v2.py`.

## 검증 기록 (2026-11-07, 로컬 Apple Silicon 맥, Docker Desktop 28.4.0·buildx, k3d v5.9.0)
| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_multiarch_build.py` (RED: 구현 전) | 수집 실패(`parse_platforms` 없음) → 구현 후 26 passed |
| 실제 Build 모듈 `_publish`(AWS 부분 `_login`·`_ecr_digest`만 로컬 레지스트리 조회로 교체) → k3d 레지스트리 `localhost:5059` | `docker buildx build --platform linux/amd64`·`linux/arm64` 각각 push 후 `imagetools create`. 목록 `sha256:1096d276…`, `application/vnd.oci.image.index.v1+json`, `linux/amd64`·`linux/arm64`(+attestation `unknown/unknown` 2개) |
| 같은 목록 `--platform linux/amd64`로 실행 | `x86_64`(QEMU) |
| k3d `cmp58-arm`(노드 `arm64`, 별도 클러스터)에 `infra/helm/postgres`·`application`, `image.platforms=[linux/amd64, linux/arm64]` | `sandbox-postgres-0`·`demo-app` Running/Ready, 노드 조건 `["amd64","arm64"]`, 앱 로그 `aarch64`, imageID = 목록 Digest |
| 같은 클러스터에 DB `platforms=[linux/amd64]`(11-07 설정 재현) | `Pending Unschedulable … didn't match Pod's node affinity/selector` |
| 정리 | `k3d cluster delete cmp58-arm`, `k3d registry delete cmp58-reg`. `k3d-cmp-local`은 건드리지 않음 |
| `make check` (ruff + pytest) | 3108 passed, 10 skipped |

## 미검증
- 데모 서버(AL2023, classic 저장소, x86_64)에서 binfmt 설치·arm64 빌드·ECR 목록 push: 서버 반영 승인 필요. ECR의 목록 `describe-images`·IMMUTABLE 태그 동작은 AWS 문서 근거이고 실제 계정에서 확인하지 않았다.
- `make smoke-temporal`: 이 worktree에 로컬 인프라 `.env`가 없어 실행하지 않았다. Workflow 코드는 바꾸지 않았고, Temporal을 지나는 값(샌드박스 `images[].platforms`, `PlanningRequestV2.builds[].platforms`)은 이미 목록 형태라 모양이 같다.
- 빌드 시간(위험): arm64는 QEMU 에뮬레이션이라 느리다(네이티브 확장 컴파일이 있으면 몇 배). Build 모듈의 외부 명령 시간 제한은 명령당 600초(기존)이고, 두 플랫폼 빌드가 차례로 돈다. Sandbox Workflow의 `sbx_build` Activity 제한은 30분(박승철님 코드, 바꾸지 않음)이다. 데모 앱으로 서버에서 실측해야 한다. 넘으면 `EXTERNAL_COMMAND_FAILED`(재시도 1회)로 끝나고, 그때 선택지는 `BUILD_PLATFORM`을 다시 한 항목으로 두는 것이다.

## 서버 반영 절차 (승인 대기, 오케스트레이터)
1. 이 PR 병합 후 릴리스(`bash infra/demo/release.sh <SHA>`) — `start.sh`가 `build-binfmt.sh install`을 실행한다. `.env`를 바꾸기 전 첫 릴리스에서도 설치를 시도한다(실패는 경고).
2. `/opt/cmp/.env`: `BUILD_PLATFORM=linux/amd64,linux/arm64`(첫 항목 = 샌드박스 호스트 아키텍처).
3. Deploy demo(릴리스 재실행)로 worker가 새 설정을 읽는다. 이때 binfmt 설치 실패는 릴리스 실패다.
4. `bash infra/demo/runtime-check.sh` → `build_binfmt_service=enabled`, `builder_amd64=yes`·`builder_arm64=yes`, `build_platforms_supported=yes`, `build_platform_first_is_host=yes`, `imds_guard_rules=1`(54 유지).

## 문서 변경
- `specs/provisioning/shared-contracts.md`: 멀티 아키텍처 빌드(58) 절
- `specs/provisioning/plan.md`: 빌드·Sandbox 순서 절에 플랫폼 목록 한 줄
- `tickets/provisioning/b-01-image-build.md`·`28-*.md`: 후속 메모
- `infra/demo/README.md`: `BUILD_PLATFORM` 안내, binfmt 점검
- `specs/analysis-verification/runbook.md`: `BUILD_PLATFORM` 예시 값
- OpenAPI: 변경 없음(`BuildEvidenceV2.platforms`는 이미 목록. `BuildAttempt`·`ValidatedImageV2`는 HTTP 계약이 아니다)

## 관련 문서
- [B-01](b-01-image-build.md) · [28](28-build-registry-runtime.md) · [54](54-build-isolation.md) · [51](51-agent-rollout-recovery.md)(#155) · [`infra/demo/README.md`](../../infra/demo/README.md)
