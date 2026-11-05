# Ticket 43 — worker 이미지에 Terraform·Helm·catalog 경로 포함과 Linux Provider lock

## 상태
IN_PROGRESS — [Issue #178](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/178). 2026-11-06 실동작 점검 D-01·D-02(로컬 compose 실측)에서 발견했다. 우선순위 P0. 21·28 후속.

## 배경
- worker 이미지는 `pip install --no-deps .`로 패키지를 site-packages에 설치한다. 그런데 코드는 `Path(__file__).parents[N]`로 저장소 루트를 찾는다. 컨테이너 안에서 이 값은 `/usr/local`이 되어 `/usr/local/infra/terraform/aws`, `/usr/local/infra/helm/application` 같은 없는 경로가 된다.
  - AWS Plan은 템플릿을 못 찾아 `MISSING_PROVIDER_LOCK`으로 거절된다.
  - `infra/helm`은 이미지에 복사되지도 않아 ON_PREM Plan, AWS·GCP WORKLOADS Plan, 차트 렌더가 실패한다.
  - 분석·Sandbox가 쓰는 `catalog/`·`skills/`도 이미지에 없다.
- 경로를 맞춰도 `.terraform.lock.hcl`에 macOS `h1` 해시 하나만 있어 Linux Terraform이 Provider를 거부한다(`PLAN_COMMAND_FAILED`).
- 근거: `infra/demo/Dockerfile:13-18`, `apps/worker/src/cmp_worker/plan_publish.py:81-83`, `packages/providers/src/cmp_providers/{aws_apply.py:26,gcp_apply.py:36,aws_preview.py:314,gcp_preview.py:322}`, `apps/worker/src/cmp_worker/workers/analysis.py:29`, `workers/sandbox.py:42`. 점검 보고서 D(`claude/prov-audit-d-cloud-e2e-2fc1b2` 브랜치).

## 관련 요구사항
PROV-001, PROV-004, PROV-008, PROV-021

## 선행 작업
Blocked by: 21 (로컬 완료), 28 (로컬 완료). 41(#176, compose·README)과 README가 겹친다.

## 작업 범위
- 포함
  - 저장소 리소스 루트를 한 곳(`cmp_providers.resources.resource_root()`)에서 찾고, 경로를 쓰는 모든 지점이 그 값을 쓰게 한다.
  - `infra/demo/Dockerfile`이 런타임 리소스 디렉터리를 복사하고 리소스 루트 환경변수를 지정한다.
  - 모든 Terraform 루트(`infra/terraform/aws`, `infra/terraform/gcp`)의 lock 파일에 `linux_amd64`·`linux_arm64`·`darwin_arm64`·`darwin_amd64` 해시를 넣는다.
  - Dockerfile COPY 목록과 코드가 요구하는 리소스 디렉터리의 대조 테스트, 환경변수 반영 테스트, lock 플랫폼 테스트, 로컬 이미지 실측.
- 제외: 권한(46·47), 웹 시작 입력(44), compose 설정(45), Provider 버전 변경, CI의 Linux `terraform validate` 작업(후속 제안).

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 환경변수 | `CMP_RESOURCE_ROOT`(절대 경로). 비어 있으면 지금처럼 소스 트리 기준(`cmp_providers` 패키지에서 4단계 위)으로 찾는다. 상대 경로면 `ValueError`로 바로 실패 | 개발·테스트(소스 트리 실행)는 설정 없이 그대로 동작한다. 상대 경로는 작업 디렉터리에 따라 결과가 바뀌어 거부한다. `pip install -e .`로 소스 위치를 유지하는 대안은 이미지에 소스 트리를 그대로 두는 것과 같아 장점이 없고, 설치 방식에 경로가 묶인다 |
| D2 공통 함수 위치 | `packages/providers/src/cmp_providers/resources.py`. 필요한 디렉터리 목록 `RUNTIME_RESOURCE_DIRS`도 여기 둔다 | Terraform 템플릿을 쓰는 providers와 차트·catalog를 쓰는 worker가 모두 이미 `cmp_providers`를 import한다. 새 패키지를 만들지 않는다 |
| D3 분석·Sandbox 최소 변경 | `workers/analysis.py`·`workers/sandbox.py`(박승철님 파일)는 저장소 루트를 구하는 한 줄만 `resource_root()`로 바꾸고 import를 더한다. `ANALYSIS_RESOURCES_DIR`가 있으면 그 값을 먼저 쓰는 기존 우선순위는 그대로 둔다 | 담당 범위를 존중한다. 이 한 줄이 없으면 이미지 안에서 `catalog`·`skills`·Sandbox 차트를 못 찾는다(D-01). 동작 변화는 "설정이 없을 때의 기본 루트"뿐이다 |
| D4 이미지 | `api` 단계(공통 기반)에서 `infra/terraform`·`infra/helm`·`catalog`·`skills`를 `/app` 아래 같은 상대 경로로 복사하고 `ENV CMP_RESOURCE_ROOT=/app` | api도 같은 패키지를 설치하므로 경로 해석이 같아야 한다. 추가 크기는 수백 KB다. `.dockerignore`가 `.terraform`·State·Plan을 이미 제외한다 |
| D5 Provider lock | Terraform 1.17.0 공식 이미지(`hashicorp/terraform:1.17.0`)로 `terraform providers lock -platform=linux_amd64 -platform=linux_arm64 -platform=darwin_arm64 -platform=darwin_amd64`를 각 루트에서 실행해 다시 만든다. Provider 버전(aws 6.71.0, google 7.46.2)은 그대로 | 데모 EC2는 x86_64, 로컬 맥·arm64 컨테이너는 arm64다. darwin_amd64는 인텔 맥 팀원용. 버전을 바꾸지 않아 기존 `zh` 해시는 그대로 남는다 |
| D6 대조 테스트 | 코드가 쓰는 디렉터리 목록(`RUNTIME_RESOURCE_DIRS`)과 Dockerfile `COPY` 대상·`ENV`를 테스트로 대조한다. 실제 이미지 실측은 로컬 `docker build`로 하고 기록한다(CI에서 이미지 빌드는 하지 않는다) | 이미지 빌드는 수 분과 외부 다운로드가 필요해 일반 테스트에 넣지 않는다. 정적 대조로 "디렉터리 추가 후 COPY 누락"을 막는다 |

## 수용 기준
- [x] worker 이미지 안에서 AWS·GCP·ON_PREM Plan 게시가 경로·lock 때문에 실패하지 않는다(권한 오류 전까지 진행). — 로컬 이미지: 경로 존재, `terraform init -backend=false -lockfile=readonly && terraform validate`(aws·gcp), `helm template`(application·postgres) 통과. 실제 Plan 게시는 자격 증명이 없어 미실행(아래 미검증)
- [x] 분석·Sandbox 리소스 경로도 이미지 안에서 존재한다. — 이미지에서 `catalog`·`skills`·`infra/helm/{postgres,application}` 확인

## Test Seam
`tests/test_worker_image_resources.py`: `resource_root()`의 환경변수·기본값·상대 경로 거부, 새 프로세스에서 환경변수가 템플릿·차트·분석 경로 상수에 반영되는지, Dockerfile COPY·ENV와 `RUNTIME_RESOURCE_DIRS` 대조, lock 파일의 네 플랫폼 `h1` 해시. 로컬 worker 이미지 실측(아래 검증 기록).

## 검증 기록 (2026-11-06)
| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_worker_image_resources.py` (RED: 구현 전) | 수집 실패(`cmp_providers.resources` 없음) → 구현 후 COPY·ENV·lock 4건 실패 → Dockerfile·lock 수정 후 10 passed |
| `make check` (ruff + pytest) | 1858 passed, 8 skipped |
| `docker run hashicorp/terraform:1.17.0 providers lock -platform=linux_amd64 -platform=linux_arm64 -platform=darwin_arm64 -platform=darwin_amd64` (aws·gcp) | 각 lock에 `h1` 3개 추가(총 4), Provider 버전·`zh` 해시 변경 없음 |
| `docker build -f infra/demo/Dockerfile --target worker` (arm64 맥, Docker 28.4, 결과 `linux/arm64`) | 성공 |
| 이미지 안: `RUNTIME_RESOURCE_DIRS` 존재, `TEMPLATE_ROOT`·`GCP_TEMPLATE_ROOT`·`DB_CHART`·`APP_CHART`·분석 루트 | 모두 `/app/...`, 존재 `True` |
| 이미지 안: `cp -r /app/infra/terraform/<aws,gcp> /tmp && terraform init -backend=false -lockfile=readonly && terraform validate` | 둘 다 `Success! The configuration is valid.` |
| 이미지 안: `helm template`(application·postgres, `tests/fixtures/contracts` 값, application은 DB host·Secret을 채운 사본), `package_chart(DB_CHART/APP_CHART)` | 렌더 성공, 패키지 2733·3180 bytes |
| 이미지 안: 실제 `plan_aws_infrastructure`(`LOCAL_VALIDATION`, 기본 템플릿 경로, 진짜 Terraform, 신원 확인 `aws`만 고정 응답 셸) | init·validate 통과 후 `plan`에서 `PLAN_COMMAND_FAILED`(자격 증명 없음 — 권한 단계). 같은 이미지에 origin/main lock을 넣으면 `validate`에서 실패(D-02 재현) |

## 미검증
- 데모 서버(x86_64) 이미지에서의 실측: 원격 반영 승인 필요. 로컬은 arm64 맥(Docker 28.4)에서 `linux/arm64`로 빌드했다.
- 실제 자격 증명으로 Plan 게시 끝까지(권한 오류 지점): 46·47 범위와 원격 승인 필요.
- GCP·ON_PREM Plan 게시를 이미지 안에서 함수 단위로 끝까지 돌리지는 않았다. GCP는 `terraform validate`, ON_PREM·워크로드는 차트 렌더·패키징까지 확인했다(자격 증명·Planning 입력 필요).
- CI에서 Linux `terraform init -lockfile=readonly && validate`를 돌리는 작업은 넣지 않았다(후속 제안). lock 플랫폼은 정적 테스트가 지킨다.

## 문서 변경
- `infra/demo/README.md`: 이미지 리소스·`CMP_RESOURCE_ROOT`
- `docs/engineering/testing-strategy.md`: Terraform lock 플랫폼 규칙
- `tickets/provisioning/21-*.md`, `28-*.md`: 후속 링크
- spec·plan·shared-contracts: 외부 계약·경계 변경 없음(문서 변경 없음)

## 관련 문서
- [21](21-demo-provisioning-runtime.md) · [28](28-build-registry-runtime.md) · [`infra/demo/README.md`](../../infra/demo/README.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인 (데모 서버 실측 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 — 외부 API·도메인 경계·계약 변경 없음. 새 설정 `CMP_RESOURCE_ROOT`(배포 설정)만 추가. OpenAPI·DB 변경 없음
- [ ] 구현·계약 정합성 및 최종 리뷰 — 조정 세션 리뷰 대기
- [x] 미검증 항목과 결과 증거 기록

## 변경 요약
- `packages/providers/src/cmp_providers/resources.py`(신규): `resource_root()`, `RUNTIME_RESOURCE_DIRS`
- `aws_apply.py`·`gcp_apply.py`·`aws_preview.py`·`gcp_preview.py`, `cmp_worker/plan_publish.py`: 루트를 `resource_root()`로
- `cmp_worker/workers/analysis.py`·`workers/sandbox.py`(박승철님 파일): 기본 루트 한 줄 + import만(D3)
- `infra/demo/Dockerfile`: `infra/helm`·`catalog`·`skills` 복사, `ENV CMP_RESOURCE_ROOT=/app`
- `infra/terraform/{aws,gcp}/.terraform.lock.hcl`: 네 플랫폼 `h1`
- `tests/test_worker_image_resources.py`(신규)
