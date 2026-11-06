# Ticket 21 — 데모 플랫폼에서 Provisioning 실행 환경 구성

## 상태
IN_PROGRESS — [Issue #100](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/100). 2026-11-05 Provisioning 공백 점검에서 발견했다. 로컬 구현·검증 완료, 데모 서버 반영(원격 변경 승인 필요)과 서버 스모크(18 완료 후)가 남았다.

## 목적
Provisioning 코드는 로컬·k3d·실제 클라우드에서 검증됐지만, 데모 서버(`infra/demo`)에서는 실행할 수 없다. worker 이미지에 실행 도구가 없고, 승인·실행 Activity가 업무 DB와 산출물에 접근하지 못하며, 온프레미스 Agent가 붙을 공개 HTTPS 주소가 없다. 데모 서버에서 Plan 생성부터 세 대상 실행까지 실제로 돌게 한다.

## 관련 요구사항
PROV-008, PROV-009, PROV-015, PROV-016, PROV-021

## 선행 작업
Blocked by: 15 (DONE). 18과 병렬 가능. 공개 주소·TLS 방식은 Traffic(중계 서버) 구성과 겹치지 않는지 확인.

## 현재 상태 (`infra/demo`)
- worker 이미지(`python:3.11-slim`)에 terraform·helm·kubectl·aws CLI·gcloud 없음
- worker에 `DATABASE_URL` 없음 → 승인·실행 Activity가 업무 DB에 접속 불가
- 산출물 저장소: 로컬 디렉터리(`ARTIFACT_STORE_DIR`, `provisioning_artifact_root`)만 구현, API·worker가 공유하지 않음. S3 미구현
- API가 `127.0.0.1:8000`으로만 열림, HTTPS 없음 → Agent 접속 불가
- `APPROVAL_ADMIN_KEY`·Agent 관리자 키·클라우드 자격 증명 설정 없음

## 작업 범위
- 포함
  - worker 이미지에 버전·체크섬 고정한 terraform·helm·kubectl·aws CLI·gcloud 추가
  - worker·API 공통 설정: `DATABASE_URL`, 관리자 키, 공유 산출물 저장소(공유 볼륨 또는 S3 중 결정), Provisioning 산출물 경로
  - 클라우드 자격 증명 주입 방식(EC2 인스턴스 역할·GCP 서비스 계정 키 파일 마운트 등). 저장소·이미지에 자격 증명을 넣지 않는다
  - Agent용 공개 HTTPS 진입점(`/agent/*`만 외부 공개, 관리자·승인 API는 제한)
  - 데모 실행·점검 문서와 스모크 절차
- 제외: Plan 생성 로직(18), 중계 서버·Envoy·DNS(Traffic), 프론트 배포, 운영급 고가용성

## 착수 결정 (2026-11-05)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 산출물 저장소 | 공유 볼륨 유지. 호스트 `/opt/cmp/data/artifact-store`(digest 이름 rollout 바이트, `ARTIFACT_STORE_DIR`)와 `/opt/cmp/data/plans`(`<plan_id>-r<revision>/`, `PROVISIONING_ARTIFACT_ROOT`)를 api·worker에 **같은 경로**로 마운트 | 단일 EC2라 볼륨으로 충분하고 `LocalArtifactStore` 코드를 그대로 쓴다. S3 구현은 `apps/` 변경이 필요하고 18과 충돌 위험이 있어 보류(다중 호스트가 필요해질 때 재검토) |
| D2 공개 HTTPS | 같은 EC2의 Caddy(`edge`, 이미지 digest 고정)가 443에서 ACME TLS-ALPN-01로 인증서를 받는다. 포트 80·DNS API·Route 53을 쓰지 않는다. 도메인은 Elastic IP 기반 `<EIP>.sslip.io` 기본, 팀 도메인이 생기면 `CMP_PUBLIC_HOST`만 바꾼다. `CMP_PUBLIC_HOST`가 설정된 경우에만 기동(opt-in) | ALB+ACM은 도메인·Route 53 검증과 ALB 비용이 필요하다. Traffic(#61–#63)의 중계 서버는 별도 호스트의 Envoy·WireGuard이고 P0에서 도메인·TLS·Route 53(`dns_finalize`)을 제외했으므로 포트·DNS·인증서가 겹치지 않는다. 중계 서버는 만들지 않는다 |
| D3 공개 경로 | 실제 Agent 경로 접두사 `/agents/*`만 전달하고 나머지는 404. `/agent-enrollments`, `/agent-admin/*`, `/tunnel-admin/*`, `/provisioning/*`, `/migrations/*`, `/docs`, `/openapi.json`, `/health`는 공개하지 않는다 | 티켓 본문의 `/agent/*`는 Agent 클라이언트(`remote_agent.py`)가 쓰는 `/agents/...`를 뜻한다. 관리자 경로는 루프백 API(SSM 포트 포워딩) + 관리자 키의 이중 보호 |
| D3a 공개 경로 변경 (2026-11-06, [#225](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/225)) | D3을 바꾼다. 대회 데모에서 브라우저가 실제 플랫폼으로 이전을 시작하고 SSE를 받도록 `/migrations`, `/migrations/*`, `/provisioning/*`도 API로 보낸다(같은 출처, CORS 없음). `/migrations/*/events`는 버퍼링 없이 흘린다(`flush_interval -1`). `/migrations`에는 인증이 없다: 대회 데모라 공개를 감수한다(후속: 인증). `/agent-enrollments`, `/agent-admin/*`, `/tunnel-admin/*`, `/provisioning-admin/*`, `/connections`, `/env-files`, `/docs`, `/openapi.json`, `/health`는 계속 공개하지 않는다(웹이 답한다) | `/provisioning/*` 아래 승인·미리보기·결과·rollout 조회는 승인자 Bearer, 승인자 등록·Plan 입력은 `APPROVAL_ADMIN_KEY`로 계속 보호된다. `/migrations/{id}/approve`(scaffold Signal)는 실제 경로(대상 있는 시작)의 Plan 승인을 대신하지 못한다(실제 경로는 승인 child가 Bearer 승인만 받는다). `/migrations/{id}/cancel`·조회·로그는 Workflow ID(UUID)를 아는 사람이면 누구나 부를 수 있다 |
| D4 관리자 키 | `APPROVAL_ADMIN_KEY`·`AGENT_ENROLLMENT_ADMIN_KEY`를 서버 `/opt/cmp/.env`(0600)에 처음 기동 시 생성하고 재배포 때 보존(`ensure-secrets.sh`, 기존 값 덮어쓰기 없음). **API에만** 주입, worker에는 넣지 않는다 | worker는 키를 쓰지 않으므로 최소 권한. 기존 서버 `.env`에 없는 키만 추가된다 |
| D5 AWS 자격 증명 | EC2 인스턴스 역할을 이름 붙은 프로필 `cmp-demo-provisioning`(`credential_source = Ec2InstanceMetadata`, 선택적 `role_arn`=`PROVISIONING_AWS_ROLE_ARN`)으로 노출. 설정 파일은 서버에서 생성해 읽기 전용 마운트(`AWS_CONFIG_FILE`). 컨테이너가 IMDSv2에 닿도록 hop limit 2 | 코드가 `default`가 아닌 이름 붙은 프로필을 요구하므로(05/08/10) 코드 변경 없이 맞춘다. 정적 키 없음. Provisioning 권한(EKS·VPC·IAM 등)은 인스턴스 역할에 아직 없다 → 별도 승인 후 역할/정책 추가 |
| D6 GCP 자격 증명 | 운영자가 서비스 계정 키를 서버 `/opt/cmp/secrets/gcp/key.json`(0600)에 직접 둔다. worker 진입점이 tmpfs(`/root/.config/gcloud`)에 gcloud 구성 `cmp-demo`를 만들고 ADC 기본 파일을 쓰며, `PROVISIONING_GCP_ADC_EMAIL`을 키의 `client_email`로 설정. 키가 없으면 GCP 실행은 기존 검사로 실패(fail closed) | `GOOGLE_APPLICATION_CREDENTIALS`는 06/10 코드가 거부하므로 ADC 기본 파일 경로를 쓴다. 키는 이미지·저장소·Temporal 이력에 없다. Workload Identity Federation은 설정 비용이 커서 보류 |
| D7 worker 이미지 | 다단계 Dockerfile `api`(기존 slim)·`worker` 대상. Terraform 1.17.0(05/08/10 검증 버전), kubectl v1.37.1·Helm v4.3.1(15 Agent와 동일), AWS CLI 2.35.21, gcloud 588.0.0 + 같은 버전 스냅샷의 `gke-gcloud-auth-plugin`. amd64·arm64 SHA-256 고정 | Terraform·kubectl·Helm은 공식 SHA-256. AWS CLI·gcloud는 SHA-256 파일을 공개하지 않아 2026-11-05 공식 URL에서 받은 파일의 해시를 고정 |
| D8 Apply 비용·정리 책임 | 실제 Apply는 Provisioning 담당(민서)이 검토한 Plan 승인 후에만 실행. 대상 리소스 비용·정리는 테스트 전략과 08/10의 수동 정리 규칙을 따르며 자동 destroy 없음. 데모 서버의 EIP·443 개방·IAM 권한 추가는 별도 승인 후 반영 | 공개 IPv4·EIP는 비용이 발생한다. 대회 후 정리는 README 비용 절에 기록 |

## 수용 기준
- [x] 데모 서버의 worker가 terraform·helm·kubectl·aws·gcloud를 실행하고, 승인·실행 Activity가 업무 DB와 산출물에 접근한다. — 로컬: worker 이미지 빌드·도구 실행, compose의 `DATABASE_URL`·공유 볼륨 검사. 서버 반영은 미검증
- [x] 노트북의 Agent(15 Chart)가 공개 HTTPS 주소로 등록·작업 수신에 성공한다. — 경로 전달은 로컬 Caddy 스모크로 확인. 공인 인증서·실제 Agent 등록은 서버 반영 후 확인(미검증)
- [x] 관리자·승인 API는 외부에 공개되지 않거나 인증으로 보호된다. — 에지는 `/agents/*`만 전달(로컬 스모크), API는 루프백 + 관리자 키
- [x] 자격 증명·키가 이미지·저장소·로그에 없다. — 읽기 전용 마운트·tmpfs, 진입점 출력 검사, 정적 키 금지 테스트
- [ ] 데모 서버에서 스모크(Plan 게시 → 승인 → ON_PREM 실행)가 통과한다(18 완료 후). — **대기**: 18 미완료, 원격 반영 승인 필요

## Test Seam 후보
이미지 빌드 후 도구 버전 확인, compose 설정 테스트(필수 환경변수·포트 노출 검사), 데모 서버에서 Agent 접속·스모크 실행.

## 검토 시 정할 것
착수 결정 D1(저장소)·D2(HTTPS·도메인)·D5/D6/D8(자격 증명·비용·정리)로 결정했다.

## 관련 문서
- [15](15-agent-runtime-install.md) · [18](18-onprem-plan-publish.md) · [43](43-worker-image-packaging.md)(후속: 이미지 안 템플릿·차트 경로와 Linux Provider lock — 이 티켓은 도구 설치만 확인해 놓쳤다) · [`infra/demo/README.md`](../../infra/demo/README.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인 (서버 스모크 제외)
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영 — 외부 API 계약·도메인 경계 변경 없음. OpenAPI 변경 없음(라우트 변경 없음). 데모 배포 문서(`infra/demo/README.md`)에 실행·점검 절차 추가
- [ ] 구현·계약 정합성 및 최종 리뷰 — 조정 세션 리뷰 대기
- [x] 미검증 항목과 결과 증거 기록

## 변경 요약
- `infra/demo/Dockerfile`: `api`/`worker` 다단계. worker에 버전·체크섬 고정 CLI 5종과 `gke-gcloud-auth-plugin`, 진입점
- `infra/demo/compose.yaml`: api·worker 공통 `DATABASE_URL`·공유 산출물 경로, API 전용 관리자 키(필수), 자격 증명 읽기 전용 마운트·tmpfs, `edge`(Caddy, `public` 프로필, 443만 공개)
- `infra/demo/Caddyfile`, `agent-routes.caddy`: TLS-ALPN-01 인증서, `/agents/*`만 전달 (이후 `/relays/*`(#167), `/migrations[/*]`·`/provisioning/*`(#225, D3a) 추가)
- `infra/demo/ensure-secrets.sh`: `.env` 누락 키만 생성, 인스턴스 역할 AWS 프로필 파일 생성
- `infra/demo/worker-entrypoint.sh`: GCP 서비스 계정 키를 tmpfs gcloud 구성·ADC로 활성화
- `infra/demo/start.sh`: 위 스크립트 사용, `CMP_PUBLIC_HOST`가 있을 때만 에지 기동, 상태 검사에 worker 도구 실행 추가
- `infra/demo/cloudformation.yaml`(**미반영**): SG 443 인그레스, IMDS hop limit 2, Elastic IP·출력
- `tests/test_demo_runtime.py`: compose·이미지·에지·CloudFormation·스크립트 Test Seam 14건
- `apps/`·`packages/` 변경 없음

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_demo_runtime.py` (macOS, Python 3.11) | 14 passed |
| `make check` (ruff + pytest) | 966 passed, 4 skipped |
| `docker compose -f infra/demo/compose.yaml config -q` (가짜 값) / `APPROVAL_ADMIN_KEY` 비움 / `--profile public config --services` | 통과 / `required variable APPROVAL_ADMIN_KEY is missing` 오류 / `edge`는 `public` 프로필에서만 포함 |
| `caddy validate` (caddy 2.10.2, 실제 Caddyfile) | `Valid configuration` |
| 로컬 에지 스모크: 실제 `agent-routes.caddy` + `tls internal` + 경로 에코 업스트림, `curl --path-as-is` | `/agents/register`, `/agents/a1/jobs/claim` → 업스트림 200. `/agents/../provisioning/plans`, `/agents/%2e%2e/agent-admin/...`, `/agent-enrollments`, `/agent-admin/*`, `/tunnel-admin/*`, `/provisioning/*`, `/docs`, `/openapi.json`, `/health`, `/migrations`, `/agentsX`, `/agents` → 404 |
| `docker build --target worker` (arm64, Docker 28.4) | 성공(1.97 GB). 다운로드 5건 모두 `sha256sum -c` OK |
| 컨테이너 도구 버전 | Terraform v1.17.0, Helm v4.3.1, kubectl v1.37.1, aws-cli 2.35.21, Google Cloud SDK 588.0.0, gke-gcloud-auth-plugin v0.1.0-gke.3; `cmp_worker`·`cmp_api` import 성공 |
| 컨테이너에서 `ensure-secrets.sh`가 만든 AWS 설정 읽기 + 설정 해석 | `aws configure list-profiles` → `cmp-demo-provisioning`, `credential_source` → `Ec2InstanceMetadata`; worker/API 설정이 공유 경로·DB 호스트를 같은 값으로 읽음. IMDS 자격 증명 획득 자체는 EC2 밖이라 미검증 |

### 미검증·제한 사항
- 데모 서버 반영(CloudFormation 갱신, 릴리스 배포), 공인 인증서 발급, 노트북 Agent의 공개 주소 등록·작업 수신, 서버 스모크(18 이후)는 실행하지 않았다. 원격 변경은 사용자 승인 후 진행한다(README 「Provisioning 실행 환경」 절의 순서).
- amd64 이미지는 빌드하지 않았다(로컬은 arm64). amd64 체크섬은 공식 파일 기준으로 고정했다.
- 인스턴스 역할에는 아직 Provisioning(EKS·VPC·IAM·S3 State) 권한이 없다. 권한 범위·역할 분리(`PROVISIONING_AWS_ROLE_ARN`)는 승인 필요.
- 06/10의 ADC 검사는 gcloud 사용자 ADC로 검증됐다. 서비스 계정 ADC 파일로 `gcloud auth application-default print-access-token`·Terraform Google Provider가 동작하는지는 실제 키로 확인하지 않았다.
- `sslip.io`의 Let's Encrypt 발급 한도(공유 도메인)는 확인하지 않았다. 막히면 팀 도메인을 `CMP_PUBLIC_HOST`로 지정한다.
- 실행 중 Terraform Apply가 있을 때 worker 교체 절차는 README의 기존 제한대로 미구현이다.
