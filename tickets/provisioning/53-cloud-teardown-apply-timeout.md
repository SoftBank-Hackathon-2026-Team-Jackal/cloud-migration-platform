# Ticket 53 — 클라우드 대상 정리(destroy)와 Apply 타임아웃 안전 처리

## 상태
IN_PROGRESS (로컬 구현·테스트·실제 terraform 로컬 실측 완료, 리뷰 대기) — [Issue #188](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/188). 우선순위 P1(비용·State 안전), 담당 민서. 08·09·10·11·14·46·52 후속. **실제 AWS·GCP 리소스는 만들지도 지우지도 않았다(승인 대기).**

## 배경 (2026-11-06 실동작 점검 D, 이전 N9·N24)
- **D-11 (P1, 비용):** Terraform 밖에서 생기는 리소스가 State에 없다. AWS LB Controller가 Ingress로 만드는 ALB·Security Group, GKE Ingress의 GCE LB·NEG, `reclaimPolicy: Retain` StorageClass의 EBS·PD가 그렇다. ALB ENI가 남으면 VPC 삭제가 막힐 수 있다. GKE `deletion_protection` 기본값이 `true`라 destroy가 그대로는 실패한다.
- **D-12 (P1):** Apply 타임아웃(90분)이 `proc.kill`(SIGKILL)로 끝난다. S3 `.tflock`이 남거나, 만들던 리소스가 State에 기록되지 않는다. 그러면 다음 실행이 lock 오류나 `SAVED_PLAN_STALE`이 된다.
- **N9 (클라우드 부분):** 대상 자원을 정리하는 경로(Destroy Scope·Activity·API)가 코드에 없다. 인프라 Apply 뒤 워크로드가 실패·거절·만료되면 EKS/GKE가 그대로 남는다. 14 F6의 "검토된 Destroy Plan"은 문서에만 있었다.

## 관련 요구사항
PROV-008, PROV-009, PROV-016, PROV-021

## 선행 작업
08, 09, 10, 11, 14, 46(DONE·병합), 52(ON_PREM 정리, 병합)

## 범위
- 포함: 클라우드(AWS·GCP) 정리 Workflow·Activity와 운영자 API, 정리 순서(Helm uninstall → LB·Ingress 삭제·소멸 대기 → PVC·디스크 정책 → 검토된 Terraform destroy), GKE 보호 해제, Terraform 타임아웃·Activity 취소 시 SIGTERM → 유예 → SIGKILL, 남은 lock 확인·보고, State 보존(`errored.tfstate`)
- 제외: 대상 권한·역할 템플릿(47), GCP 실행 주체 검사(48), ON_PREM 정리(52), 정리 시점을 정하는 Migration Workflow 단계(Traffic·데이터 담당 합의 필요), 남은 비용 리소스를 태그로 찾는 점검 Activity(후속), lock 강제 해제 자동화(운영 절차만), Worker 재배포 graceful shutdown(N19, 후속)

## 수용 기준 (Issue #188)
- [x] 정리 순서가 Activity로 구현되고 운영자 API로만 시작된다. — 가짜 kubectl·Helm·terraform runner 테스트로 순서·실패·재시도(멱등) 확인
- [x] 데이터 자원은 명시 요청(`delete_data=true`) 없이 지우지 않는다. 기본은 PVC·Retain 디스크를 남기고 ID를 보고한다.
- [x] Terraform 타임아웃은 SIGTERM → 유예 → SIGKILL로 끝나고, 남은 lock과 State 보존 여부를 결과에 남긴다. — 실제 terraform(1.10.5·1.17.0) 로컬 백엔드 실측
- [x] destroy는 46 규칙(대상 = 연결 `credential_ref`, State backend = 플랫폼 프로필)을 따른다.
- [ ] 실제 EKS·GKE 정리 실측 — **승인 대기**(클라우드 리소스 생성·삭제 금지)

## Test Seam
- 프로세스 종료: `cmp_providers.terraform_process.run_graceful`(실제 `sleep`·`python` 자식 프로세스), `aws_apply.subprocess_runner`·`terraform_events.streaming_runner`: `tests/test_terraform_process.py`
- Apply 중단 결과: `apply_aws_infrastructure`·`apply_gcp_infrastructure` + 기존 `FakeRunner`: `tests/test_aws_apply.py`, `tests/test_gcp_apply.py`
- destroy: `cmp_providers.cloud_destroy.plan_destroy`·`apply_destroy` + 가짜 runner: `tests/test_cloud_destroy.py`
- 워크로드 정리: `cmp_worker.activities.cloud_teardown.CloudTeardownActivities`(ActivityEnvironment, 가짜 ClusterSession): `tests/test_cloud_teardown.py`
- Workflow: `CloudTeardownWorkflow`(Temporal 테스트 환경, 가짜 Activity): `tests/test_cloud_teardown.py`
- 운영자 API: `TestClient` + 가짜 Temporal handle: `tests/test_cloud_teardown_api.py`
- 실제 terraform: `scripts/smoke_terraform_interrupt.py`(로컬 백엔드 `terraform_data` + `local-exec sleep`, apply·destroy·타임아웃)
- 놓치는 것: 실제 AWS LB Controller·GKE Ingress 컨트롤러의 LB 삭제 시간, EBS·PD CSI 삭제, S3 `.tflock` 실제 객체, GKE `deletion_protection` 해제 Apply

## 착수 결정 (2026-11-06)
| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| D1 시작 경로 | 운영자 API만: `POST /provisioning-admin/teardowns` `{plan_id, revision, delete_data}`(관리자 키, 52와 같은 `require_admin`). 대상은 **이미 실행된 INFRASTRUCTURE/FULL Plan Revision에서만 파생**한다(State key·Spec·대상 ID). 요청이 임의 클러스터·버킷을 고를 수 없다. 자동 정리(실패·취소·만료 뒤)는 하지 않는다 | 14 F6, `temporal-flow.md` §11(취소 ≠ 삭제), 52 D7과 같은 원칙. 정리 시점을 Migration Workflow에 넣는 것은 Traffic·데이터 담당 합의 뒤 같은 Workflow를 자식으로 부르면 된다 |
| D2 실행 단위 | Temporal Workflow `CloudTeardownWorkflow`(ID `cloud-teardown-<plan_id>-r<revision>`, 같은 ID 실행 중이면 409 `TEARDOWN_IN_PROGRESS`). 상태는 Workflow Query로 본다(`GET /provisioning-admin/teardowns/{teardown_id}`). 업무 DB 테이블을 더하지 않는다(Alembic 변경 없음) | 정리는 운영자 조치이고 결과는 Workflow 이력과 운영 로그로 충분하다. 승인 기록(`provisioning_executions`)의 의미(생성 실행 표식)를 바꾸지 않는다 |
| D3 순서 | ① 워크로드: 승인된 WORKLOADS/FULL Revision(같은 대상)에서 Namespace·Release를 모아 앱 → DB 순서로 `helm uninstall --ignore-not-found` ② 클러스터 전체의 `Ingress`와 `type: LoadBalancer` Service를 지우고 **객체가 사라질 때까지** 기다린다(컨트롤러 finalizer가 클라우드 LB를 지운 뒤에 객체가 사라진다) ③ PVC·디스크 정책(D4) ④ Destroy Plan → 운영자 확인 → destroy(D5) | Ingress는 Helm 밖(`kubectl apply`)에서 만들어 uninstall로 지워지지 않는다. LB Controller를 먼저 지우면 finalizer가 영원히 남는다(컨트롤러는 클러스터와 함께 지운다). 클러스터는 이 환경 전용(P0, 기존 네트워크 미지원)이라 LB 객체 전부가 이 환경 비용이다. ②·③이 끝나지 않으면 destroy로 넘어가지 않는다 |
| D4 PVC·디스크 | 기본 `delete_data=false`: PVC·PV를 그대로 둔다. Retain PV의 디스크 ID(`csi.volumeHandle`)를 `retained_volume_ids`로 보고한다. 클러스터를 지워도 디스크는 남는다. `delete_data=true`: 플랫폼 Namespace의 PVC마다 묶인 PV를 `reclaimPolicy: Delete`로 바꾸고 PVC를 지워 CSI가 디스크를 지우게 한 뒤 PV가 사라질 때까지 기다린다. 다른 Namespace의 Retain PV는 지우지 않고 보고만 한다 | `policies.md`(데이터 자원 자동 삭제 금지), 52 D8(PVC 기본 보존). Retain StorageClass(09·11)는 PVC를 지워도 디스크를 남기므로 데이터 삭제는 PV 정책 변경이 필요하다. 디스크를 클라우드 API로 직접 지우지 않아 대상 계정 권한(47)을 넓히지 않는다 |
| D5 검토된 destroy | Activity `plan_cloud_destroy`가 Spec(승인 Revision의 `spec.json`, Digest 확인)으로 변수 파일을 다시 만들고 `terraform plan -destroy`를 한다. 모든 변경이 `delete`가 아니면 `FAILED DESTROY_PLAN_UNSAFE`. 검토 Digest = sha256(정규 JSON{환경, 삭제 주소 목록, 보호 해제 여부}). Workflow는 운영자 확인 Signal(`POST …/{teardown_id}/confirm {review_digest}`)을 **최대 24시간** 기다리고, 없으면 `UNKNOWN DESTROY_NOT_CONFIRMED`로 끝낸다(워크로드 정리는 이미 됨). 확인 뒤 `apply_cloud_destroy`가 **다시** `plan -destroy`를 만들어 주소 목록이 검토본과 같을 때만 그 Plan을 Apply한다(다르면 `FAILED DESTROY_PLAN_CHANGED`) | 14 F6의 "검토된 Plan(Destroy)". 저장 Plan은 State serial에 묶여 GKE 보호 해제 Apply 뒤 stale이 되므로, 바이트가 아니라 "지울 주소 집합"을 검토 대상으로 고정한다. 템플릿 변경(drift)은 destroy를 막지 않고 `template_drift=true`로 보고한다 |
| D6 GKE 보호 해제 | Destroy Plan의 prior state에서 `google_container_cluster.deletion_protection=true`면 `-var=deletion_protection=false` Plan을 따로 만들어 **그 클러스터의 `deletion_protection` 한 속성 update만** 있는지 확인한다(아니면 `FAILED UNPROTECT_PLAN_UNSAFE`). 검토 Digest에 `unprotect=true`를 넣고, 확인 뒤 보호 해제 → destroy 순서로 Apply한다 | 14에서 수동으로 한 순서(1 change → plan -destroy)다. Google provider는 State 값으로 삭제 보호를 판단해 destroy 변수만으로는 지울 수 없다 |
| D7 실행 주체 (46) | destroy도 Spec의 `credential_ref`(`resolve_aws_profiles`)로 Terraform provider·`aws sts`·`aws eks`·kubectl을 실행하고 S3 backend는 `-backend-config=profile=<플랫폼>`이다. 시작 전에 대상 계정(`ACCOUNT_MISMATCH`)·State 계정(GCP `STATE_ACCOUNT_MISMATCH`)·ADC 주체(GCP, `gcp_apply.default_adc_email`, 48이 바꾸면 그대로 따름)를 확인한다 | 생성과 삭제가 같은 주체여야 대상 계정 밖을 건드리지 않는다. 48·47 범위의 함수는 import만 하고 고치지 않는다 |
| D8 타임아웃 종료 | `terraform_process.run_graceful`: 시한(또는 Activity 취소)에 **SIGTERM** → 유예(`TERRAFORM_GRACE_SECONDS`, 기본 300초) → SIGKILL. `subprocess_runner`(init·state·output·destroy)와 `streaming_runner`(apply -json) 둘 다 이 함수를 쓴다. 결과 `CommandResult.stopped` = `TERMINATED`(유예 안에 끝남)·`KILLED` | 실측: Terraform 1.10.5·1.17.0 모두 SIGTERM에 "Interrupt received… Gracefully shutting down"으로 끝나며 lock을 풀고 진행 중이던 자원까지 State에 기록했다. SIGKILL은 lock 파일을 남기고 진행 중 자원을 State에서 잃었다. SIGINT도 같은 처리지만 Kubernetes·Docker가 보내는 종료 신호와 맞춰 SIGTERM을 쓴다 |
| D9 시간 예산 | Apply 시한 90분 + 유예 5분 + 확인 조회 < Activity `start_to_close` 2시간. 실행 중 30초마다 heartbeat(기존), heartbeat 시한 5분. Activity가 취소되면(Workflow 취소·heartbeat 시한) 같은 graceful 종료를 하고 결과를 기다린 뒤 `UNKNOWN TERRAFORM_APPLY_INTERRUPTED`를 남긴다. 재시도는 기존대로 없음(Apply·destroy `maximum_attempts=1`). 워크로드 정리는 멱등이라 3회 | 중단 결과를 반드시 검사(lock·State)한 뒤 사람에게 넘긴다(plan.md §12). 자동 재실행은 lock 경합만 만든다 |
| D10 lock·State 보고 | 중단·실패 뒤: `state_lock` = `HELD`/`ABSENT`/`UNKNOWN`(S3 `<state_key>.tflock`을 `aws s3api head-object`, 플랫폼 프로필로 조회), `stopped`, `state_resource_count`(기존). 작업 폴더에 `errored.tfstate`가 있으면 지우지 않고 `<artifact_root>/state-recovery/<plan>-r<rev>-<시각>.tfstate`(0600)로 옮기고 `state_recovery=PRESERVED`를 남긴다. lock은 자동으로 풀지 않는다(운영 절차: 실행 중 프로세스가 없음을 확인한 뒤 `terraform force-unlock <ID>`) | Terraform은 원격 State 저장에 실패하면 작업 폴더에 `errored.tfstate`를 쓰는데, 지금은 `finally`에서 폴더를 지워 그 State를 잃는다. lock 강제 해제는 다른 실행을 깨뜨릴 수 있어 사람이 판단한다 |
| D11 상호 배제 | 같은 대상(`manifest.target`)에 `RUNNING` 실행 표식이 있으면 409 `EXECUTION_IN_PROGRESS`. 정리 Workflow가 이미 돌면 409 `TEARDOWN_IN_PROGRESS`. 생성 쪽 동시 실행은 State lock이 마지막 방어선이다 | 정리 중 새 Apply가 시작되는 경합은 사람의 순서 조정 문제로 두고, 승인 실행 쪽 사전 차단은 후속(정리 상태를 업무 DB에 남길 때) |
| D12 결과 코드 | 성공 `SUCCEEDED DESTROYED`(정리할 것이 없으면 `NOTHING_TO_DESTROY`). 실패 `FAILED` `DESTROY_PLAN_UNSAFE`·`UNPROTECT_PLAN_UNSAFE`·`DESTROY_PLAN_CHANGED`·`DESTROY_REVIEW_MISMATCH`·`TERRAFORM_DESTROY_FAILED`·`ACCOUNT_MISMATCH` 등. 불명확 `UNKNOWN` `LOAD_BALANCER_DELETE_PENDING`·`VOLUME_DELETE_PENDING`·`DESTROY_NOT_CONFIRMED`·`TERRAFORM_DESTROY_INTERRUPTED`·`CLUSTER_ACCESS_FAILED`. 결과에는 코드와 비밀이 아닌 ID만 담는다 | 08 E4와 같은 결과 형식. 명령 출력은 결과에 넣지 않는다 |

## 함수 경계 (47·48과 겹치는 파일)
- `aws_apply.py`: 53은 `subprocess_runner`(→ `run_graceful`)와 `apply_aws_infrastructure`의 `applied.returncode != 0` 분기 안(lock·State 보고), 작업 폴더 `finally` 앞의 `errored.tfstate` 보존만 고친다. 47은 `_checked_inputs`·계정 확인·프로필·역할 템플릿을 고친다.
- `gcp_apply.py`: 53은 같은 실패 분기와 `finally`만 고친다. 48은 `default_adc_email`·주체 검사를 고친다. destroy는 이 함수들을 import해 쓴다.
- `terraform_events.py`: 53은 `streaming_runner`만 `run_graceful`로 바꾼다.

## 구현 위치
- Provider: `packages/providers/src/cmp_providers/terraform_process.py`(신규: graceful 종료·lock 조회·State 보존), `cloud_destroy.py`(신규: Destroy Plan·검토 Digest·보호 해제·Apply), `aws_apply.py`·`gcp_apply.py`·`terraform_events.py`(위 경계)
- Worker: `apps/worker/src/cmp_worker/activities/cloud_teardown.py`(신규: 워크로드 정리·destroy Activity), `k8s_workloads_cli.py`(정리용 kubectl·Helm 메서드), `workflows/cloud_teardown.py`(신규), `workers/provisioning.py`·`workers/migration.py`(등록), `aws_infra.py`·`gcp_infra.py`(취소 시 graceful 종료)
- API: `apps/api/src/cmp_api/cloud_teardown.py`(신규: 대상 파생·상호 배제), `routes/cloud_teardown.py`(신규), `main.py`
- 실측: `scripts/smoke_terraform_interrupt.py`(신규)

## 검증
- 단위·Workflow·API: `tests/test_terraform_process.py`, `tests/test_cloud_destroy.py`, `tests/test_cloud_teardown.py`, `tests/test_cloud_teardown_api.py`, 바뀐 `test_aws_apply`·`test_gcp_apply`
- RED: 새 테스트가 구현 전 실패(모듈 없음, SIGKILL만 보냄, 실패 뒤 `errored.tfstate` 삭제)
- 실제 terraform: 아래 표
- `make check`·`make openapi`: 결과는 PR 본문
- Alembic: 변경 없음
- `make smoke-temporal`: 로컬 Temporal이 있을 때만(PR 본문)

## 미검증
- 실제 EKS·GKE 정리 전체(ALB·GCE LB·NEG 소멸 시간, EBS·PD CSI 삭제, VPC 삭제, GKE 보호 해제 Apply): **승인 대기**
- 실제 S3 `.tflock`이 SIGTERM·SIGKILL 뒤 남는지: 로컬 백엔드 lock 파일로만 확인
- Worker 프로세스 자체 종료(N19) 중의 Apply

## 문서 변경
- `specs/provisioning/plan.md` §8·§12, `specs/provisioning/shared-contracts.md`(정리 경로), `docs/architecture/temporal-flow.md` §11, 티켓 08·10·14에 53 후속 표시, OpenAPI 재생성

## 완료 체크리스트
- [x] 티켓·tasks.md 행·Issue 연결
- [ ] RED → GREEN 단위 테스트
- [ ] 실제 terraform 로컬 실측
- [ ] SDD 문서 갱신
- [ ] 리뷰·병합(오케스트레이터)
- [ ] 실제 클라우드 정리 실측(사용자 승인)
