# Ticket 48 — GCP 대상 플랫폼 주체와 GCP 연결 확인

## 상태
IN_PROGRESS — [Issue #183](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/183). 로컬 구현·검증 완료. 리뷰, GCP 주체 생성(승인 대기 명령), 키 배치·서버 반영, 실제 키로 tokeninfo 확인이 남았다.

## 배경 (점검 B-06·B-07·D-07)
- B-06: `sbh26-prod`에 플랫폼이 쓸 서비스 계정이 없다. 기본 compute SA뿐이고 프로젝트 역할·사용자 관리 키가 없다. 노드 풀 입력 `node_service_account_email`의 SA를 누가 만드는지도 정해지지 않았다.
- D-07(추정): 주체 확인은 `gcloud auth application-default print-access-token` 토큰의 tokeninfo `email`을 비교한다. 서비스 계정 키 ADC 토큰은 기본 scope가 `cloud-platform` 하나라 tokeninfo에 `email`이 없다. 그래서 데모 키로는 `ADC_PRINCIPAL_MISMATCH`가 난다(`gcp_preview.py` `_verify_adc`, `gcp_apply.py` `default_adc_email`).
- B-07(추정): API 컨테이너에는 GCP 키·ADC·gcloud가 없다(41 D1). 그래서 `GCP_TARGET` 연결 확인(`routes/connections.py` `_adc_checker`)이 늘 실패한다.

## 관련 요구사항
PROV-003, PROV-007, PROV-021, PROV-022

## 선행 작업
Blocked by: 06(ADC 검사), 10(GCP Apply), 21(데모 worker 진입점), 23(연결 API), 46(연결 프로필, main 병합). 순서 의존 없음.

## 작업 범위
- 포함: GCP 플랫폼 SA·노드 SA·최소 역할 정의와 생성 스크립트(기본 dry-run), 키/WIF 결정, ADC 주체 확인(서비스 계정 키·WIF·사용자 ADC), GCP 연결 확인 위치 이동(API → worker Activity), 데모 문서의 GCP 줄.
- 제외: AWS(47), destroy·정리(53), 분석·ENV compose(45), 실제 SA 생성·IAM 바인딩·키 발급·서버 반영(승인 대기), 사용자 프로젝트 WIF 연결(ADR-005 목표, 후속).

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| G1 플랫폼 SA | `cmp-provisioning@sbh26-prod.iam.gserviceaccount.com`. 프로젝트 역할 `roles/container.admin`(클러스터·노드 풀, 11 워크로드의 kubectl), `roles/compute.networkAdmin`(VPC·서브넷·Router·NAT), `roles/compute.securityAdmin`(방화벽). 노드 SA에만 `roles/iam.serviceAccountUser`(리소스 바인딩, 프로젝트 전체 아님) | 템플릿(`infra/terraform/gcp/main.tf`)이 만드는 리소스 7종을 덮는 최소 미리 정의 역할이다. `roles/editor`·`owner`는 IAM 외 전부를 열어 기각. 커스텀 역할은 권한 목록 유지 비용이 커서 후속 |
| G2 노드 SA | `cmp-gke-nodes@sbh26-prod.iam.gserviceaccount.com`(10 G6·README의 이름) + `roles/container.defaultNodeServiceAccount` | GKE 권장 최소 노드 역할(로그·메트릭 쓰기). 기본 compute SA(편집자 역할 관행)는 쓰지 않는다 |
| G3 생성 방법 | `infra/demo/gcp-platform-identity.sh`. 기본은 dry-run(실행할 `gcloud` 명령만 출력), `--apply`일 때만 실행. 프로젝트·구성 이름을 확인한 뒤 실행하고 키는 만들지 않는다(키 명령은 안내만) | 한 번 만드는 IAM이라 Terraform State를 새로 둘 이유가 없다. 이미 있으면 건너뛰어 재실행해도 같다 |
| G4 키 vs WIF | **데모는 서비스 계정 키**(worker에만 읽기 전용 마운트, 진입점이 tmpfs에 ADC 작성 — 21 그대로). WIF는 ADR-005 목표로 남기되, 주체 확인 코드는 WIF credential config(`external_account` + `service_account_impersonation_url`)도 받는다 | 데모 일정 안에 Workload Identity Pool·AWS 공급자·속성 조건·가장 바인딩을 실제로 검증할 시간이 없다. 조직 정책 `iam.disableServiceAccountKeyCreation`은 이 프로젝트에 적용되지 않는다(읽기 전용 조회, `booleanPolicy: {}`). 키는 저장소·S3·Temporal 이력에 두지 않는다 |
| G5 주체 확인 | 새 모듈 `cmp_providers.gcp_adc`. ADC well-known 파일(`$CLOUDSDK_CONFIG` 또는 `~/.config/gcloud`의 `application_default_credentials.json`)에서 `type`과 선언된 주체만 읽는다. `service_account`: 토큰을 `--scopes=cloud-platform,userinfo.email`로 받아 tokeninfo `email`이 `client_email`과 같아야 한다. `external_account`·`impersonated_service_account`: 가장 URL의 SA 이메일이 주체이고, tokeninfo로 토큰이 유효해야 하며 `email`이 있으면 같아야 한다. `authorized_user`·파일 없음(GCE 메타데이터): 지금처럼 tokeninfo `email`. 그 밖의 형식·가장 없는 `external_account`는 `ADC_SOURCE_UNSUPPORTED` | gcloud 문서상 `--scopes`는 사용자·서비스 계정 ADC만 지원하고 외부 계정은 지원하지 않는다. 가장 토큰은 해당 SA가 `generateAccessToken`을 허락해야만 나오므로 URL의 SA가 실제 주체다. Plan(06)·Apply(10)·연결 확인(23)이 같은 함수를 쓴다 |
| G6 실패 코드 | 토큰 명령 실패·빈 토큰은 `ADC_UNAVAILABLE`(이전: Plan에서는 `PLAN_COMMAND_FAILED`). ADC 파일이 깨졌으면 `ADC_UNAVAILABLE`. 나머지 코드는 같다 | 연결 확인에서 `PLAN_COMMAND_FAILED`는 뜻이 맞지 않는다. 새 코드는 만들지 않는다(`ADC_*` 4종 유지, ADR-005) |
| G7 연결 확인 위치 (B-07) | **worker Activity로 옮긴다.** API는 짧은 Workflow `GcpConnectionCheckWorkflow`(`migration` 큐)를 실행하고 결과를 기다린다(최대 45초). Workflow는 `provisioning` 큐의 `verify_gcp_connection` Activity 하나만 부른다(시도 2회). Activity는 같은 `verify_adc_principal`을 쓴다. Temporal에 닿지 못하거나 시간이 지나면 `GCP_CHECK_UNAVAILABLE`, 이상한 결과는 `GCP_CHECK_FAILED`. AWS 확인은 지금처럼 API 안에서 끝난다 | GCP 자격은 이미 worker에만 있다(21·41 D1). 인터넷에 닿는 API에 장기 키와 gcloud를 더 주지 않는다. Temporal 이력에는 예상 주체(이메일)와 코드만 남는다(비밀 없음). ADR-005의 "확인은 API 프로세스 안에서 끝난다"는 AWS에 한정하도록 고쳤다. 기각: api에 키 마운트 + gcloud 설치(키 노출면 2배, api 이미지 비대), API에서 키로 JWT 직접 서명(인증 라이브러리 재구현) |
| G8 compose | api는 그대로(GCP 키·gcloud 없음, 기존 테스트가 고정). worker는 기존 키 마운트 그대로. 바뀐 것은 문서·주석뿐이다 | 위치 이동으로 api 변경이 필요 없다 |

## 수용 기준
- 서비스 계정 키 ADC: 이메일 scope로 토큰을 받고 `client_email`과 tokeninfo `email`이 같을 때만 통과한다(가짜 gcloud·tokeninfo 테스트).
- WIF·가장 ADC: 가장 URL의 SA가 기대 주체와 같고 토큰이 유효하면 통과, 다르면 `ADC_PRINCIPAL_MISMATCH`.
- 지원하지 않는 ADC 형식은 `ADC_SOURCE_UNSUPPORTED`, 토큰 실패는 `ADC_UNAVAILABLE`.
- Plan(06)과 Apply(10)가 같은 확인을 쓴다.
- `GCP_TARGET` 연결 확인이 API 프로세스의 ADC를 쓰지 않고 worker Activity 결과로 `CONNECTED`/`FAILED`가 된다(Temporal 테스트 서버로 검증). Temporal 장애는 `GCP_CHECK_UNAVAILABLE`.
- SA·역할 스크립트가 기본 dry-run이고 `--apply` 없이는 아무것도 바꾸지 않는다.

## 검증
- `make check`(ruff·pytest), `make openapi`(경로 변경 없음 확인), `bash -n`, 스크립트 dry-run 출력.
- 읽기 전용 조회: `gcloud config get-value project --configuration cmp-gcp-prod` → `sbh26-prod`, SA 목록(기본 compute SA뿐), 키 생성 조직 정책(적용 안 됨).

## 미검증
- 실제 SA 키로 받은 토큰의 tokeninfo `email`(키 발급이 승인 대기라 못 했다). gcloud 588의 `--scopes` 동작은 문서 기준이다.
- WIF credential config 실제 교환·가장(설계·코드만).
- 실제 GKE Plan/Apply가 G1 역할만으로 통과하는지(승인 범위의 실 Plan 필요).

## 승인 대기 (원격 변경)
1. `infra/demo/gcp-platform-identity.sh --apply` (SA 2개·역할 바인딩 생성).
2. 키 발급: 운영자 PC에서 `gcloud iam service-accounts keys create <임시 경로> --iam-account cmp-provisioning@sbh26-prod.iam.gserviceaccount.com --configuration cmp-gcp-prod` → SSM 세션으로 서버 `/opt/cmp/secrets/gcp/key.json`(root, 0600) → 임시 파일 삭제. `.env`에 `CMP_GCP_PROJECT=sbh26-prod`.
3. 서버 릴리스(이 PR의 worker·api 코드).

## 문서 변경
ADR-005(GCP 확인 위치·주체 규칙·데모 주체, 후속 메모), `specs/provisioning/plan.md`(23 GCP 줄), `shared-contracts.md`(48 절: `gcp_adc`, Workflow·Activity 이름, 새 연결 코드), `docs/architecture/temporal-flow.md`(GCP 연결 확인 Workflow), `infra/terraform/gcp/README.md`(ADC 형식·스크립트), `infra/demo/README.md`·`compose.yaml` 주석(GCP 줄), `tasks.md` 행. OpenAPI 경로·응답 모양 변경 없음.
