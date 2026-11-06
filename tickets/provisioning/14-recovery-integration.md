# Ticket 14 — 실패 복구와 세 대상 통합 검증

## 상태
DONE — [Issue #56](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/56); 착수 결정 F1~F8

## 목적
세 Provider 경로의 재시도·부분 실패·취소를 검증하고 안전한 결과/복구 상태를 남긴다.

## 관련 요구사항
PROV-015, PROV-016, PROV-017, PROV-018, PROV-019, PROV-021

## 선행 작업
Blocked by: 08, 09, 10, 11, 12, 13

## 작업 범위
- 포함: Terraform/Helm/Agent 중복 실행 방지와 Worker 종료 후 상태 조회, 승인 재검증, 세 대상 통합 시나리오를 구현한다.
- 제외: 무조건 Destroy, DB/트래픽 롤백의 자동화

## 수용 기준
- [x] Apply 성공 후 응답 유실·Helm 성공 후 Worker 종료·Agent 재연결에서 실제 상태 조회 후 중복 부수효과를 억제한다.
- [x] 부분 생성 자원과 실패·복구 가능성을 기록하고 취소를 자원 삭제 완료로 표시하지 않는다.
- [x] EKS/GKE/K3s 각각 검증 증거 또는 미검증 사유를 남기고 필수 검사 실패 시 다음 단계로 가지 않는다.

## 테스트 전략
- 제안 Test Seam: Migration Workflow 실행/재시작 → 외부 상태/Result. 실제 데이터·트래픽 롤백은 확인하지 못한다.
- 실행: Temporal 실패 주입 + 격리 대상 재시도 통합 테스트; 승인된 클라우드 E2E는 별도 실행.

## 완료 시 확인할 동작
세 대상의 중복 실행·부분 실패·취소 처리가 결과와 증거로 설명된다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 자동 정리 범위, 취소 후 보존, Retry/Heartbeat 수치와 실제 Cloud E2E 범위를 결정한다. → F3·F4·F6·F7로 결정.
- 구현 중 변경: 2026-11-05 착수 결정 F1~F8 참조.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## 착수 결정 (2026-11-05)

사용자 지시: 기획 문서의 최신 결정을 우선 적용하고 없는 항목은 구현자가 최선안을 정해 기록한다. 실제 검증은 사용자가 GKE 장애 주입, 로컬 K3s(k3d), EKS 재생성을 승인했다(2026-11-05). 자동 삭제는 하지 않는다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| F1 복구 원칙 | 같은 Revision은 다시 실행하지 않는다(07 claim 표식 유지). `UNKNOWN`·`FAILED` 뒤 복구는 실제 상태를 확인한 다음 **새 Revision 승인**으로만 한다. 새 Revision의 각 경로가 변경 전에 실제 상태를 조회해 중복 부수효과를 막는다: Terraform은 중앙 S3 State에서 새 Plan을 만들고(이미 생성된 자원은 변경 없음), Helm은 Release를 조회해 skip/upgrade하며 `pending-*`는 `UNKNOWN`으로 멈추고, Agent는 Revision별 job ID와 Release 조회를 쓴다. | plan §12, 08 E4·09 W8·12 E1. 같은 Revision 자동 재실행은 이전 실행이 아직 살아 있을 때 두 번 적용될 위험이 있다. |
| F2 Worker 종료 | Provider Activity는 재시도 없이(최대 1회) Heartbeat 5분·실행 2시간 제한을 유지한다. Worker가 죽으면 Heartbeat 시간 초과로 `UNKNOWN`/`PROVIDER_ACTIVITY_LOST`가 되고, 새 Worker가 Workflow를 이어받아 결과를 기록한다. DB Activity(claim/complete/handoff)는 멱등하므로 3회 재시도한다. | 08 결정, Temporal Retry는 외부 부수효과의 정확히 한 번을 보장하지 않음 |
| F3 취소 | Workflow 취소는 claim 전이면 실행 표식 없이 `CANCELLED`로 끝난다. Provider 단계 중이면 진행 중인 Activity에 취소를 요청하되, 완료 기록은 **`UNKNOWN`/`EXECUTION_CANCELLED`**로 남기고(13 결과: `CHECK_STATE_FIRST`) 자원 삭제 완료로 표시하지 않는다. 취소 후에도 complete는 반드시 실행해 `RUNNING` 표식이 남지 않게 한다. | PROV-017, plan §12 "취소만으로 무조건 삭제 금지" |
| F4 수치 | Provider: start-to-close 2시간, heartbeat 5분, 최대 1회. DB: start-to-close 2분, 최대 3회. GKE Ingress 대기 15분(11 K6), Pod/PVC 대기는 09 기본값 유지. | 실측(11: Ingress 약 14분) |
| F5 부분 자원 기록 | 실패·UNKNOWN 결과에는 실제로 기록된 refs만 남긴다(13 R3). 인프라 단계는 State 자원 수(`state_resource_count`)를 `cluster.network_refs`와 별도로 결과 `partial_resources`에 남기고, 워크로드 단계는 생성된 Release·PVC·Secret 참조를 남긴다. | PROV-017 "부분 생성 자원과 복구 가능성 기록" |
| F6 자동 정리 | 실패·취소·검증 후 자동 Destroy·Helm uninstall은 하지 않는다. 정리는 사용자가 결정하고 검토된 Plan(Destroy) 또는 명시적 명령으로만 한다. 클라우드 경로는 53(운영자 API·검토 Digest 확인), ON_PREM은 52가 구현했다. | ADR-002, 사용자 지시 |
| F7 실제 검증 범위 | **EKS:** 08 입력과 같은 사양으로 새 Plan(`plan-aws-recovery` r1)을 만들어 실제 Workflow로 Apply하는 도중 Worker를 종료해 `UNKNOWN`을 만든다. 이어서 S3 State·실제 자원을 조회하고 새 Revision(r2)으로 Plan→Apply해 중복 VPC·클러스터가 생기지 않는지 확인한다. 이후 워크로드 Revision으로 READY와 13 결과 저장까지 확인한다. **GKE:** 기존 `cmp-target-gke`의 새 Namespace `cmp-recovery-test`에서 Helm 설치 중 Worker를 종료하고 새 Revision으로 재실행해 Release revision이 늘지 않는지 확인한다. **K3s:** 격리 k3d에서 12의 Agent 오프라인·재연결·결과 유실 복구 smoke를 다시 실행하고 13 결과를 확인한다. | 사용자 승인(2026-11-05) |
| F9 복구 Plan (구현 중 추가, 2026-11-05) | 실제 EKS 검증에서 r1 Apply 응답 유실 뒤 같은 환경의 r2 Plan이 05/06 Preview 규칙(신규 전체 생성만 허용)에 걸려 `INCOMPLETE_NEW_PLAN`으로 거절됐다. 즉 F1의 "새 Revision으로 복구"가 인프라에서는 불가능했다. 그래서 **이 환경의 자체 State에 이미 있는 자원은 `no-op`(NO_CHANGE)으로 유지하고 부족한 자원만 생성하는 Plan**을 허용하고, Preview에 경고 `RECOVERY_CONTINUATION`을 붙인다. `update`·`delete`는 계속 거절하고, VPC·클러스터·노드(GCP는 7종 전부)가 CREATE 또는 NO_CHANGE로 모두 있어야 한다. 사용자가 가진 기존 네트워크를 쓰는 것이 아니라 플랫폼이 같은 State로 만든 자원만 대상이므로 ADR-002 #6(기존 네트워크 재사용 제외)과 충돌하지 않는다. | 실측 발견. 대안(UNKNOWN 뒤 수동 정리 후 신규 Plan)은 이미 생성된 클러스터를 지워야 해 비용·위험이 크다. |
| F8 Test Seam | 상위 `MigrationWorkflow`는 아직 스캐폴드이므로 실제 실행 경로인 `ProvisioningApprovalWorkflow`를 Seam으로 쓴다. Temporal 테스트 환경에서 Worker 중지·재시작, 취소, Activity 유실을 주입하고 업무 DB 실행 표식·13 결과·인계 차단을 확인한다. 실제 대상은 F7. | 티켓 Test Seam의 "Migration Workflow"는 현재 승인 Workflow가 담당 |

## 구현 (2026-11-05)

- Workflow 취소(F3): 승인 전 취소 → `CANCELLED`/`CANCELLED_BEFORE_EXECUTION`, 실행 표식 없음. claim·complete DB Activity는 `asyncio.shield`로 보호해 취소 중에도 끝까지 실행하므로 `RUNNING` 표식이 남지 않는다. Provider 단계 중 취소 → `UNKNOWN`/`EXECUTION_CANCELLED`(Activity 취소는 `ActivityError` 안의 취소 예외로 구분). claim 직후 취소면 Provider를 실행하지 않고 `FAILED`/`CANCELLED_BEFORE_EXECUTION`(사전 확인 단계)으로 기록한다.
- 부분 자원(F5): `ProvisioningResult.partial_resources`에 실패·UNKNOWN 실행이 기록한 refs(State 자원 수, Namespace, StorageClass, Pull Secret, DB Secret 위치, Release, PVC, Ingress, Agent job)만 남긴다. 성공 시 비어 있다. Activity 자체가 유실되면 refs가 보고되지 않으므로 비어 있을 수 있다(실측 GKE r1).
- 그 외 Retry·Heartbeat 수치, 복구 방식(새 Revision), 자동 정리 없음은 기존 동작을 결정으로 확정했다(F1·F2·F4·F6).

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_recovery_integration.py` | 7개 통과. 승인 전 취소(실행 없음), Provider 단계 중 취소(`UNKNOWN`·`CHECK_STATE_FIRST`·인계 차단, `RUNNING` 잔존 없음), Worker 중지 후 새 Worker 재개(`PROVIDER_ACTIVITY_LOST`, Provider 1회만 실행), 같은 승인 재전달 시 재실행 없음, 인프라·워크로드 실패의 부분 자원 기록, 성공 시 부분 자원 없음. Worker 유실·재전달 2건은 구현 전에도 통과해 기존 동작을 확인했고, 취소·부분 자원 5건은 RED → GREEN |
| `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` | Ruff·전체 517개 통과 |
| **GKE** 실제 `cmp-target-gke`, Namespace `cmp-recovery-test`, 실제 승인 Workflow(Temporal 테스트 환경) + 실제 `execute_gcp_workloads` | r1: DB Helm Release가 `deployed`(revision 1)가 된 직후 Worker 중지 → 새 Worker가 Workflow를 이어받아 `UNKNOWN`/`PROVIDER_ACTIVITY_LOST` 기록, 13 결과 `CHECK_STATE_FIRST`. r2(새 Revision, 같은 승인 Values): DB Release를 조회해 재설치하지 않고(revision 1 유지) 앱만 설치 → `WORKLOADS_READY`, 13 결과 READY. 증거: [provisioning-14-gke.json](../../docs/verification/provisioning-14-gke.json) |
| **K3s** 격리 k3d `k3d-cmp-recovery`(v5.9.0, K3s v1.36.4, ARM64), 전용 kubeconfig | ① 12 smoke 재실행: Agent 오프라인 `UNKNOWN`/`AGENT_OFFLINE` → 재연결 `READY`(Helm revision 1) → 결과 보고 유실 후 재설치 없이 복구(revision 1 유지) → 이미지 불가 Revision `FAILED`/`WORKLOAD_NOT_READY`, R4 표준 키 포함. ② 실제 승인 Workflow 경로: r1 Agent 오프라인 → `UNKNOWN`, 13 결과 저장·인계 차단. r2 새 Revision → `EXECUTED`/`READY`, 결과에 `agent_id`·`cluster_uid`, Data 인계 진행 가능·Traffic은 `EXTERNAL_ENDPOINT_UNAVAILABLE`로 차단. r2가 최신이 된 뒤 남아 있던 r1 Agent 작업은 `STALE_REVISION`으로 거절돼 배포되지 않았다. 증거: [provisioning-14-k3s.json](../../docs/verification/provisioning-14-k3s.json). 정리: Namespace·k3d 클러스터·Registry 삭제, 기본 kubeconfig 해시 동일 |
| **EKS** 재생성(계정 714066696240, ap-northeast-2), 08과 같은 검토 사양, 실제 승인 Workflow + 실제 `execute_aws_plan` | r1: Apply 시작 4분 뒤 Worker 중지 → `UNKNOWN`/`PROVIDER_ACTIVITY_LOST`(13 결과: 인프라 단계, `CHECK_STATE_FIRST`). Terraform은 Worker가 죽은 뒤 약 7분 더 실행돼 끝까지 적용했다(응답 유실). 실제 상태 조회: State serial 12·자원 29개, 잠금 해제, EKS·Node Group `ACTIVE`. r2 Plan 첫 시도는 `INCOMPLETE_NEW_PLAN`으로 거절 → F9 수정 후 r2 Plan은 17개 항목 **전부 NO_CHANGE**(생성 0)·`RECOVERY_CONTINUATION` 경고 → 승인 Workflow로 Apply `EXECUTED`/`TERRAFORM_APPLIED`, 13 결과 `INFRASTRUCTURE_READY`, 같은 VPC. 독립 확인: VPC(10.40.0.0/16)·NAT·대상 EKS·Node Group 각 1개, Subnet 4개, 클러스터 OIDC 1개(중복 없음), State 자원 29개 유지 |
| **EKS** 워크로드(09 경로, 실제 `execute_aws_workloads`) | r1 `WORKLOADS_READY`(LB Controller 3.6.2, PVC Bound 10Gi `cmp-gp3`, DB·앱 Running, 외부 ALB `/health` 200×5). r2(새 Revision, 같은 Values) `WORKLOADS_READY`, Helm Release revision 1 유지(재설치 없음). 증거: [provisioning-14-eks.json](../../docs/verification/provisioning-14-eks.json) |
| `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` (F9 포함) | 전체 521개 통과(Preview 복구 Plan 테스트 4개 추가) |

### 미검증·제한

- 실제 Temporal 서버·Worker 프로세스 강제 종료가 아니라, Temporal 테스트 서버에서 Worker를 중지하고 새 Worker를 시작해 프로세스 종료를 재현했다. 그래서 중지된 Worker 안의 Terraform/Helm 실행은 같은 프로세스의 스레드로 끝까지 진행됐다. 실제 프로세스가 죽을 때 하위 Terraform/Helm이 함께 종료되는 경우(부분 적용·잠금 잔존)는 실측하지 않았다. 그 경우에도 새 Revision Plan은 S3 State에서 만들어지며, 잠금이 남아 있으면 Plan이 실패하므로 수동 확인이 필요하다.
- GKE r1은 Activity가 유실돼 refs가 보고되지 않아 `partial_resources`가 비어 있다. 실제 생성 자원(DB Release)은 다음 Revision의 Release 조회로 확인됐다.
- `make smoke-temporal`: 2026-11-05 로컬 Temporal에서 실행했다. 저장소 스크립트는 분석 #45 이후 Sandbox Worker가 빠져 실패하므로, Sandbox Worker를 더한 같은 스크립트로 `scaffold_complete`를 확인했다(MNP-01 #51 검증 기록). 이 smoke는 `MigrationWorkflow` 스캐폴드만 실행하며, 승인 Workflow 변경은 Temporal 테스트 환경에서 확인했다.
- 상위 `MigrationWorkflow`, 실제 데이터·트래픽 롤백은 범위 밖이다.
- 검증 자원은 F6에 따라 자동 삭제하지 않았고, 아래 "검증 자원 정리"처럼 사용자 결정으로 정리했다. 로컬 k3d 클러스터·Registry는 검증 직후 삭제했다.

## 검증 자원 정리 (2026-11-05, 사용자 결정)

사용자 지시: "원본 소스는 지우지 말고, 지난 티켓 검증을 위해 만든 자원은 모두 삭제". 원본 `cmp-demo-source`(EKS·ALB·EIP·ECR 이미지), 중앙 S3 State 버킷, 다른 팀 자원(`sbh26-cdc-poc*`), AWS 기본 VPC는 유지했다.

| 대상 | 방법 | 결과 |
| --- | --- | --- |
| EKS `cmp-demo-target` 워크로드 | Ingress 삭제(LB Controller가 ALB·Target Group 제거) → Helm uninstall → Namespace·PV 삭제 → Retain EBS `vol-08c9…` 삭제 | ALB·Target Group·EBS 0 |
| EKS 인프라(10.40.0.0/16) | 08 템플릿·같은 변수·S3 State로 `terraform plan -destroy`(33개 삭제만, 대상 `cmp-target-aws`·`vpc-0320…` 확인) → Apply | `33 destroyed`, State 자원 0, EKS는 `cmp-demo-source`만, IAM `cmp-target*` 역할·클러스터 OIDC 제거 |
| GKE `cmp-demo-target`·`cmp-recovery-test` | Ingress 삭제(GCE LB·NEG·방화벽 제거) → Helm uninstall → Namespace·PV 삭제 → Retain PD 2개 삭제 | forwarding rule·proxy·URL map·backend·NEG·`pvc-*` 디스크 0 |
| GKE 인프라 | 검토된 Plan으로 `deletion_protection` true→false만 변경(1 change) → `plan -destroy`(7개 삭제만, 전부 `cmp-target-gke-*`) → Apply | `7 destroyed`, State 자원 0, 클러스터·VPC·Router·NAT·NAT IP 없음 |
| 이전 티켓 잔존물 점검 | 미부착 EBS, ELB·Target Group, EIP, 스냅샷, IAM, GCP 디스크·주소·방화벽 조회 | 잔존물 없음. 남은 ALB·EIP 2개는 원본 `demo-source` ALB 소유 |
| GKE 노드 SA `cmp-gke-nodes` | 자동 승인 검토에서 비가역 삭제로 거부돼, 사용자가 직접 `gcloud iam service-accounts delete` 실행 | 삭제 확인. 프로젝트에는 기본 Compute SA만 남음 |

