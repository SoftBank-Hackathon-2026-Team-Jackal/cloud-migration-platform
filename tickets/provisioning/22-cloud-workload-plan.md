# Ticket 22 — AWS/GCP 앱·DB 배포 Plan 게시와 09·11 실행 연결

## 상태
DONE — [Issue #108](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/108). 2026-11-05 조율 세션 지시로 착수 결정을 기록하고 구현했다. 자체 리뷰 완료, 조율 세션 리뷰·PR 대기.

## 목적
18(#98)은 AWS/GCP에서 `INFRASTRUCTURE` Plan만 게시한다(18 결정 P2). 그래서 클라우드 이전은 인프라 Apply(08·10) 뒤 DB·앱 배포(09·11) 없이 다음 scaffold 단계로 넘어가고, 13 인계에 쓸 DB·앱 결과가 생기지 않는다. 인프라 실행 결과를 입력으로 워크로드 Plan·Preview를 게시하고, 승인 후 09·11 배포를 실행해 클라우드 이전이 "인프라 → 워크로드 → 인계"까지 이어지게 한다.

## 관련 요구사항
PROV-007, PROV-008, PROV-010, PROV-011, PROV-012, PROV-013, PROV-015, PROV-018

## 선행 작업
Blocked by: 05, 06, 08, 09, 10, 11, 18 (DONE)

## 현재 구현과 재사용 지점
- `cmp_worker.plan_publish.PlanPublisher`(18): Planning 입력 등록 → Plan·Preview 게시, 입력 Digest 재사용
- `execute_aws_workloads`·`execute_gcp_workloads`(09·11): `WORKLOADS` scope Plan을 승인 기준으로 재검증해 배포. 클러스터는 `ExecutionRefsResolver`가 같은 대상 식별자의 최신 `SUCCEEDED` INFRASTRUCTURE 실행 refs에서 찾는다(09 W11, 11 K12). 산출물은 `PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/<sha256>`에서 읽는다
- 09·11 앱 Values는 v1 `ApplicationValues` 템플릿(동적 DB host·Secret 슬롯 + `DynamicBinding`)이다. ON_PREM(12)의 v2 앱 Values와 형식이 다르다
- `MigrationWorkflow._plan_and_provision`(18 P7): Plan 게시 → Revision 승인 자식 Workflow → 실행 결과
- 13 `ProvisioningResult`: Plan Revision별 결과, `get_migration_handoff({plan_id, revision, consumer})`

## 범위
- 포함
  - AWS/GCP 대상 설정에 워크로드 설정(Namespace·StorageClass·DB 이미지) 추가(선택 필드)
  - 인프라 Revision의 실행 결과(refs)를 입력으로 워크로드 Plan(`WORKLOADS`)·Preview를 만들고 게시하는 Activity
  - `MigrationWorkflow` provisioning 단계: 클라우드 인프라 실행 성공 뒤 워크로드 Plan 게시 → 승인 대기 → 09·11 실행 결과
  - 마이그레이션 Plan 조회 API가 워크로드 Plan이 있으면 그것을 돌려주게 조정
  - SDD·Temporal 흐름·공유 계약 문서 갱신
- 제외: `.env` 값 주입(20), Sandbox, 다른 팀 단계(Data·Traffic·Analysis)의 연결, 실제 클라우드 Plan·Apply(별도 승인), 16의 터널·인계 필드

## 수용 기준 (Issue #108)
- [x] AWS/GCP 각각 인프라 실행 결과로 워크로드 Plan·Preview가 게시되고 승인 후 09·11 배포가 시작된다.
- [x] 같은 입력 재실행은 중복 Revision을 만들지 않는다. 인프라 결과가 바뀌면 새 Revision이 된다.
- [x] 05·06 증거·08·10 Apply 검사와 기존 18 경로에 회귀가 없다.
- [x] 게시·실행 결과가 13 결과 계약(`ProvisioningResult`)으로 남아 Data/Traffic 인계가 진행된다.
- [x] Plan·Preview·로그·Temporal 이력에 Secret 원문이 없다.

## Test Seam
- 게시 → 실행 → 인계: SQLite 업무 DB + 로컬 ArtifactStore + 실제 Helm 패키징·렌더 + 05/06 가짜 Terraform으로 인프라 Plan 게시 → 07 승인·claim·complete(08/10 refs 형식) → `publish_workloads` → 07 승인 → **실제** `execute_aws_workloads`/`execute_gcp_workloads` Activity(가짜 클러스터, 실제 `ExecutionRefsResolver`) → complete → `get_migration_handoff`(DATA/TRAFFIC `proceed`)
- 재실행 판정: 같은 인프라 결과 재게시는 `PLAN_REUSED`, 새 인프라 Revision 실행 결과는 새 워크로드 Revision
- Workflow: Temporal time-skipping 서버의 실제 `MigrationWorkflow`·`ProvisioningApprovalWorkflow`와 가짜 Provisioning Activity. 클라우드 인프라 → 워크로드 승인 → 실행, 워크로드 게시 실패·거절, 18 이전 이력(`tests/fixtures/temporal/migration-aws-infra-only-v1.json`, 이 티켓 착수 전 코드로 만든 이력) Replay
- 놓치는 것: 실제 클라우드·클러스터(09·11에서 실제 확인됨), 실제 서버(21)

## 착수 결정 (2026-11-05)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| W1 Scope | 인프라 실행 성공 뒤 **두 번째 Plan(`WORKLOADS`)** 을 게시한다. 단일 `FULL` Plan은 쓰지 않는다 | 워크로드 Plan 입력이 인프라 Apply 결과(클러스터 refs)여야 하는데 FULL은 Apply 전에 만들어진다. FULL은 05·06 증거(`evidence.json` manifest Digest)와 08·10 Apply 검사를 바꿔야 한다(18 P2). 09·11은 이미 `WORKLOADS`를 지원하고 클러스터를 인프라 실행 refs로 찾는다. 승인자는 실제 클러스터 위의 DB·앱 변경을 따로 본다 |
| W2 Plan ID | 워크로드 Plan은 Migration당 별도 ID `workload_plan_id_for(migration_id)` = `plan-` + `sha256("WORKLOADS:" + migration_id)` 앞 24자. 인프라 Plan ID(18 P2)는 그대로 | 승인·실행은 Plan별 최신 Revision만 허용한다. 같은 Plan ID에 섞으면 인프라 재게시·재사용 판정(18 P3)이 워크로드 Revision과 엉킨다 |
| W3 워크로드 설정 | `AwsTargetSettings`·`GcpTargetSettings`에 선택 필드 `workloads: {namespace, storage_class, database_image}`를 추가한다(`settings` 밖, `settings`는 05/06 입력 그대로). 없으면 워크로드 게시가 `WORKLOAD_SETTINGS_MISSING`으로 실패하고 Migration은 `plan_failed`로 끝난다(이미 만든 인프라는 지우지 않는다). 인프라 입력 Digest는 `workloads`를 빼고 계산해 기존 등록·인프라 Revision 재사용이 바뀌지 않는다 | Provisioning 소유 등록 계약의 하위 호환 추가다. Namespace·StorageClass는 Terraform 출력이 아니라 배포 선택값이고, DB 이미지는 Digest 고정이 필요해 기본값을 두지 않았다(코드 기본값 없음 유지. 2026-11-06 [44](44-web-start-to-planning.md#착수-결정-2026-11-06) S6: 웹 시작은 운영자 대상 Profile 카탈로그의 `workloads`를 쓴다). StorageClass는 09·11이 없으면 만들고 Provisioner가 다르면 거절한다 |
| W4 인프라 결과 입력 | Workflow가 실행된 인프라 Revision `{plan_id, revision}`을 넘기고, Activity가 업무 DB에서 그 Revision의 manifest·실행 표식을 다시 읽는다. 같은 Migration·Provider의 `INFRASTRUCTURE`이고, 인프라 Plan의 최신 Revision이며, 모든 실행 단계가 `SUCCEEDED`이고, refs에서 09·11의 클러스터 대상(`target_from_refs`)을 만들 수 있어야 한다. 아니면 `INFRASTRUCTURE_NOT_FOUND`·`INFRASTRUCTURE_STALE`·`INFRASTRUCTURE_NOT_SUCCEEDED`·`INFRASTRUCTURE_OUTPUTS_MISSING`. 워크로드 manifest의 `target`은 인프라 manifest의 `target`과 같다 | Temporal 이력에는 ID만 남고 refs는 DB가 원본이다(13 R7). `target`이 같아야 09·11 resolver가 그 인프라 실행을 찾는다 |
| W5 재실행 판정 | 워크로드 입력 Digest = `sha256_json({registration: 18 입력 Digest, workloads, infrastructure: {plan_id, revision, manifest_digest, resource_refs}, template_version: "cloud-workloads-v1"})`. 최신 Revision과 같으면 `PLAN_REUSED`, 다르면 새 Revision(18 P3와 같은 저장 규칙) | 같은 인프라 결과는 같은 Revision, 인프라가 새 Revision으로 다시 실행되면(refs·Digest 변경) 새 Revision이 된다 |
| W6 Values·산출물 | DB Values는 04B `render_shared_deployment_v2`(Provider AWS/GCP, `PRODUCTION`)로 만들고 두 Chart를 `helm template`으로 확인한다. 앱 Values는 09·11 계약인 v1 `ApplicationValues` 템플릿으로 변환한다: v2 리소스 출력 바인딩 → `DATABASE` env(`host`·`port`·`name`·`username`·`password_secret_ref→secret_ref`·`connection_url_secret_ref`), `config_ref`=`environment.env_file_ref`, DB 연결 슬롯은 비워 둠. BINDINGS는 `prepare_application`의 `DynamicBinding` 목록. 09·11과 같은 값으로 해석한 Values를 `render_application`으로 렌더해 확인한다. Release·DB Secret 이름은 18 P4 규칙(`<db release>-auth`/`password`). 산출물은 09·11이 읽는 `PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/<sha256>`에 쓴다(같은 이름에 다른 바이트가 있으면 `PLAN_DIRECTORY_CONFLICT`) | 09·11의 `_verify_values` 검사를 게시 시점에 통과하는 바이트만 게시한다. 사용자 `.env`는 20 |
| W7 Preview | `cmp_providers.cloud_workload_preview`(순수 함수), 05/06/12A와 같은 `Preview` 계약. `changes`: CLUSTER(인프라 실행이 만든 클러스터·Namespace, `NO_CHANGE`), STORAGE(StorageClass, `READ`; 없으면 Provider Provisioner로 생성, 다르면 거절), DATABASE·APP·INGRESS(이 워크로드 Plan에 성공한 실행이 없으면 `CREATE`, 있으면 `UPDATE`). `applied_defaults`: Namespace·DB 비밀번호 Secret 생성. `warnings`: `CLUSTER_STATE_NOT_INSPECTED`. Values 대상·이미지 불일치는 `errors`(게시 차단). Planning 입력의 `pending_decisions`를 그대로 넣는다 | 게시 시 클러스터를 조회하지 않는다(외부 I/O 없음). Secret은 이름·키만 |
| W8 Workflow | `MigrationWorkflow._plan_and_provision`에서 인프라 결과가 `EXECUTED`이고 게시 결과가 AWS/GCP `INFRASTRUCTURE`일 때만 `workflow.patched("provisioning-cloud-workloads-v1")` 아래 `workload_planning` 단계로 `publish_workload_plan` Activity를 부른다. `PUBLISHED`면 18과 같은 Revision 승인 자식으로 승인 대기·실행 결과를 기다린다. 결과 처리(거절·만료·실패·취소)는 18 P7과 같고, 게시 실패는 `plan_failed`. 상태 Query의 `plan`은 현재 Plan(워크로드), `infrastructure_plan`은 인프라 Plan ID·Revision. ON_PREM과 18 이전 이력은 patch 표식이 없어 기존 경로로 재생된다 | 다른 단계·Workflow 시작 계약·`MigrationStatus`는 바꾸지 않는다. 자식 시작·대기 코드는 helper로 옮기되 명령 순서는 그대로다 |
| W9 인계 | 13 코드는 바꾸지 않는다. 워크로드 Revision의 `ProvisioningResult`(`WORKLOADS`)가 DB·앱 인계를 담고, Data/Traffic은 Workflow 상태 `plan`의 워크로드 Plan ID·Revision으로 `get_migration_handoff`를 부른다 | 13 R7 그대로. 16이 바꾸는 인계 필드와 겹치지 않는다 |
| W10 Plan 조회 API | `GET /provisioning/migrations/{migration_id}/plan`은 워크로드 Plan이 게시돼 있으면 그 최신 Revision을, 없으면 인프라 Plan을 돌려준다. 응답 형식은 그대로 | 승인자가 지금 승인할 Plan을 받는다. 인프라 Revision은 `GET /provisioning/plans/{plan_id}/revisions/{revision}/preview`로 계속 볼 수 있다 |

## 구현 요약
- 계약: `cmp_contracts.plan_publish`의 `CloudWorkloadSettings`, `AwsTargetSettings`/`GcpTargetSettings.workloads`(선택), `workload_plan_id_for`, `WORKLOAD_PATCH`·`WORKLOAD_TEMPLATE`. 18 입력 Digest는 `workloads` 제외(`cmp_api.plan_inputs.input_digest`)
- 게시: `PlanPublisher.publish_workloads`(인프라 결과 확인·Values 변환·렌더 확인·산출물 쓰기·Preview), 공통 `_publish_revision`(18 재사용 판정을 두 Plan이 함께 씀), `deployment_request(..., provider=)`
- Preview: `cmp_providers.cloud_workload_preview`
- Activity: `publish_workload_plan`(`provisioning` queue, 저장소 미설정 시 `SCAFFOLD`), provisioning Worker에 등록
- Workflow: `MigrationWorkflow._plan_and_provision`(patch `provisioning-cloud-workloads-v1`) + 승인 대기 helper `_approve_and_execute`(18 코드 이동, 명령 순서 동일), 상태 Query `infrastructure_plan`
- API: `GET /provisioning/migrations/{migration_id}/plan`이 워크로드 Plan 우선(W10). 응답 스키마 변경 없음
- DB 스키마 변경 없음(등록 JSON에 선택 키만 추가). 13·16 코드(`tunnels`, `get_migration_handoff`, 인계 모델)는 건드리지 않았다

## 검증 기록 (2026-11-05)
- `make check`: ruff lint/format 통과, `pytest` 1083 passed, 6 skipped
- 신규 `tests/test_cloud_workload_plan.py`(8): AWS·GCP 각각 가짜 Terraform 인프라 게시 → 07 승인·claim·complete(08/10 refs) → 워크로드 게시(`WORKLOADS`, 인프라와 같은 `target`, `approval_issues` 없음) → 07 승인 → 실제 `execute_aws_workloads`/`execute_gcp_workloads`(가짜 클러스터, 실제 인프라 refs resolver)가 `WORKLOADS_READY` → complete → `get_migration_handoff` DATA·TRAFFIC `proceed`. 클러스터에서 만든 DB 비밀번호가 Plan·Preview·실행 refs·인계·산출물 파일에 없음. Activity 게시·미설정 `SCAFFOLD`. 같은 인프라 결과 `PLAN_REUSED`·새 인프라 Revision 실행 결과는 새 Revision. 워크로드 설정 추가가 인프라 Revision·18 Digest를 바꾸지 않음. 미실행·실패·없음·이전 Revision·refs 없음·설정 없음·미등록·ON_PREM은 게시하지 않고 코드 반환. Plan 조회 API가 게시 뒤 워크로드 Plan을 돌려줌
- 신규 `tests/test_cloud_workload_workflow.py`(5, Temporal time-skipping + Replayer): AWS·GCP 인프라 승인 → 실행 → 워크로드 게시(입력은 ID만) → 승인 → 09/11 실행 → `scaffold_complete`(상태 `plan`=워크로드, `infrastructure_plan`=인프라), 워크로드 게시 실패 `plan_failed`, 워크로드 거절 `plan_rejected`, 이 티켓 착수 전 코드로 기록한 AWS 인프라 이력(`tests/fixtures/temporal/migration-aws-infra-only-v1.json`) Replay
- 기존 회귀: 18 `test_plan_publish`(20)·`test_migration_plan_workflow`(6, ON_PREM 경로·기존 이력 Replay), 05·06 Preview, 08·10 Apply, 09·11 Workloads, 13 결과, 14 복구 테스트 통과
- `make openapi`: `CloudWorkloadSettings` 스키마, AWS/GCP 대상 설정의 선택 `workloads`, Plan 조회 route 설명만 바뀐 diff 검토
- `alembic check`: DB 스키마 변경이 없어 실행하지 않았다

## 미검증
- `make smoke-temporal`: 로컬 Temporal(7233)이 꺼져 있어 실행하지 못했다(공유 compose 컨테이너를 새로 띄우지 않음). Workflow는 Temporal 테스트 서버에서만 확인했다. smoke는 scaffold 경로라 이 티켓의 클라우드 경로를 타지 않는다
- 실제 AWS/GCP Plan·Apply와 실제 EKS/GKE 배포는 돌리지 않았다(09·11에서 같은 Activity를 실제 클러스터로 확인함). 클라우드 비용 없음. 게시된 v1 앱 Values를 실제 클러스터의 09/11이 받는 경로는 가짜 클러스터로만 확인했다
- 실제 서버(21)에서 Planning 입력 등록에 `workloads`를 넣는 운영 절차는 21 범위로 남는다
- Data/Traffic Worker가 Workflow 상태 `plan`(워크로드 Revision)으로 인계를 읽는 연결은 각 담당 범위(현재 `copy_data`·`shift_traffic`은 scaffold)

## 관련 문서
- [18](18-onprem-plan-publish.md) · [09](09-aws-workloads.md) · [11](11-gcp-workloads.md) · [13](13-result-handoff.md) · [공유 계약](../../specs/provisioning/shared-contracts.md)

## 설계 변경 기록
- 2026-11-05: 18 P2의 후속으로 분리. 착수 결정 W1~W10. 공유 계약·Temporal 흐름·plan·tasks 문서 갱신. spec.md 요구사항 변경 없음

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영 (ADR 변경 없음)
- [x] 구현·계약 정합성 및 최종 리뷰 (자체 리뷰. 조율 세션 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록
