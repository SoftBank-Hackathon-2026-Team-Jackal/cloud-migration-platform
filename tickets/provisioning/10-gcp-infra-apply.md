# Ticket 10 — 승인된 Plan으로 GCP GKE 기반 준비

## 상태
DONE — [Issue #2](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/2) 재사용. 승인된 실제 Apply와 GKE 기동을 확인했다. G6의 노드 서비스 계정 IAM 바인딩은 사용자 결정(2026-11-05)으로 후속 처리로 미뤘다.

## 목적
승인된 저장 Plan을 적용하고 GKE 접속·Node Pool·영속 스토리지 선행 조건을 확인한다.

## 관련 요구사항
PROV-008, PROV-015, PROV-016, PROV-017

## 선행 작업
Blocked by: 06, 07

## 작업 범위
- 포함: GCP Terraform Apply/Output, State·잠금·실제 리소스 재조회와 GKE 권한/StorageClass 준비를 구현한다.
- 제외: App·PostgreSQL Helm 배포와 트래픽 전환

## 수용 기준
- [x] 승인 Revision/Digest와 대상 프로젝트/State가 다르면 Apply하지 않는다.
- [x] GKE·Node Pool·네트워크와 Kubernetes 접근 상태 및 ID를 기록한다.
- [x] 부분 실패·Worker 종료 뒤 State/실제 자원을 확인해 중복 생성을 막는다.

## 테스트 전략
- 제안 Test Seam: 승인된 GCP 실행 요청 → 인프라 상태/Output. 앱 헬스는 확인하지 못한다.
- 실행: Plan/State 불일치 및 재조회 테스트; 실제 Apply는 명시된 격리 프로젝트에서만 실행.

## 완료 시 확인할 동작
GKE 기반 준비와 재시도 가능한 상태를 조회할 수 있다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: Backend/Lock, Node·네트워크 사양, 사전 구축 여부를 먼저 정한다.
- 구현 중 변경: G1~G6 착수 결정, G6 IAM 바인딩 보류(아래 "G6 변경")

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## P0 정책 갱신 (2026-11-03)

[ADR-002](../../docs/decisions/ADR-002.md)에 따라 AWS/GCP 기존 네트워크 재사용은 제외하고 신규 구성만 실행한다. State는 중앙 S3와 잠금을 사용한다. 실제 리소스 생성은 특정 Revision의 Preview·사용자 승인 이후에만 수행한다.

## 착수 결정 (2026-11-04)

사용자 지시에 따라 기획 문서의 최신 결정을 우선 적용하고, 없는 항목은 구현자가 최선안을 정해 기록한다. 사용자는 08·09와 같은 방식의 실제 GCP 생성을 승인했다(정리는 11 이후 사용자 결정, 자동 삭제 없음).

| ID | 결정 | 근거 |
| --- | --- | --- |
| G1 실행 분기 | 08의 `execution_steps`에 (GCP, INFRASTRUCTURE)→`execute_gcp_plan`, (GCP, WORKLOADS)→`execute_gcp_workloads`(11), (GCP, FULL)→둘 다 순서대로를 추가한다. Workflow·claim/complete는 바꾸지 않는다. | 08 E1 |
| G2 Apply 입력 | 08 E2와 같다: 저장된 Plan만 Apply, Spec·Plan 바이트 Digest, 프로젝트·리전·State 식별·key(`cmp/gcp/<environment_id>/terraform.tfstate`)·State 계정, `S3` 모드 Plan만 허용(`LOCAL_TEST`/`LOCAL_PROJECT` 거절), 템플릿·lockfile 지문, ADC 주체(Worker 설정 `PROVISIONING_GCP_ADC_EMAIL`)와 S3 Backend용 AWS 계정(STS)을 확인한다. | ADR-002 #7·#10, 06 README |
| G3 중복·복구 | 08 E4와 같다. 재시도 없음, stale·중단은 `UNKNOWN`, State 자원 수만 기록, 자동 destroy 없음. | plan.md §8 |
| G4 결과 | Output(비밀 아님)과 `gcloud container clusters/node-pools describe` 상태를 `resource_refs`로 남긴다: `gcp_project_id`, `gke_cluster_name`, `gke_location`, `vpc_network`, `subnetwork`, `node_pool_name`, `gke_cluster_status`, `node_pool_status`. 둘 다 `RUNNING`이어야 SUCCEEDED. | PROV-016·021 |
| G5 템플릿 `gcp-gke-v2` | 비밀이 아닌 Output을 추가한다. `deletion_protection`을 변수(기본 `true`)로 바꿔, 나중에 정리할 때 검토된 Plan으로만 해제할 수 있게 한다. GKE는 GCE Ingress와 Persistent Disk CSI가 기본 제공되므로 08과 달리 Controller IAM이 필요 없다. | 10 범위 "GKE 권한/StorageClass 준비" |
| G6 프로젝트 선행 조건 | Terraform 밖의 일회성 준비로 `container.googleapis.com`을 활성화하고, 노드 서비스 계정 `cmp-gke-nodes`를 만들어 `roles/container.defaultNodeServiceAccount`만 부여한다(06 README의 "기존 노드 SA" 전제). 실행 명령과 결과를 검증 기록에 남긴다. 2026-11-05: IAM 바인딩은 권한 부족으로 후속 처리(아래 "G6 변경"). | 06 설계 유지, 최소 권한 |

### 합의한 Test Seam

- Seam: 승인된 GCP 실행 요청(claim) → `execute_gcp_plan` Activity → 인프라 상태/`resource_refs`. Terraform·gcloud·AWS STS·ADC 확인은 가짜 명령 러너로 바꿔 불일치·stale·실패·성공을 확인한다. 분기는 Temporal 테스트 환경에서 확인한다.
- 놓치는 것: 실제 GCP 권한·할당량·생성 시간, 실제 State 잠금 경합(승인된 실제 실행에서만 확인), 워크로드(11).

## 검증 기록 (2026-11-04)

| 명령·환경 | 결과 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` | Ruff 검사·포맷과 전체 408개 테스트 통과. 일반 샌드박스에서는 Temporal 테스트 서버 시작이 제한되어 허용된 실행 환경에서 재검사했다. |
| Terraform 1.17.0 `fmt -check` / 오프라인 Provider 7.46.2 `init -backend=false -lockfile=readonly` / `validate` | 통과. Provider 실행은 일반 샌드박스에서 제한되어 허용된 실행 환경에서 `validate`를 마쳤다. |
| `cmp-gcp-prod` / `cmp-aws-prod` 식별 | GCP 프로젝트 `sbh26-prod`, ADC `kms.dev.data@gmail.com`, 중앙 State AWS 계정 `714066696240` 확인. |
| 로컬 업무 DB·Temporal 승인 경로 조회 | `plan-gcp-target` r1: `APPROVE`·`DELIVERED`, 실행 표식 `SUCCEEDED`/`TERRAFORM_APPLIED`, Workflow 결과 `EXECUTED`. 중복 Apply는 실행하지 않았다. |
| 중앙 S3 State 읽기 전용 조회 | `cmp/gcp/target-gcp-demo/terraform.tfstate` 존재, State serial 3. 신규 Network·Subnet·Router·NAT·Firewall·GKE Cluster·Node Pool 각 1개 기록. |
| GCP·Kubernetes 읽기 전용 조회 | `cmp-target-gke`(`asia-northeast3`)와 `cmp-target-gke-primary`가 `RUNNING`, Kubernetes 노드 3개 `Ready`. `pd.csi.storage.gke.io`와 기본 `standard-rwo` StorageClass 확인. 노드 서비스 계정은 `cmp-gke-nodes@sbh26-prod.iam.gserviceaccount.com`, GKE API 활성화 확인. |
| 재검증(2026-11-05, 읽기 전용) | State serial 3·자원 7종, 클러스터·Node Pool `RUNNING`, 노드 3개 `Ready`, `standard-rwo`(PD CSI) 기본 StorageClass 동일. `make check` 408개 통과. Terraform 파일은 `bff98de` 이후 변경 없음. |
| 사용자 승인 IAM 변경 시도(2026-11-05) | `gcloud projects add-iam-policy-binding sbh26-prod --member=serviceAccount:cmp-gke-nodes@sbh26-prod.iam.gserviceaccount.com --role=roles/container.defaultNodeServiceAccount --condition=None` → 첫 시도 Resource Manager 읽기 할당량 초과, 재시도 `setIamPolicy` 권한 거부. 실행 계정 `kms.dev.data@gmail.com`의 프로젝트 역할은 `roles/editor`뿐이다. 우회하지 않았다. |

### G6 변경 (2026-11-05, 사용자 결정)

- 해커톤 P0에서는 리소스 생성·삭제 권한으로 충분하다고 판단해, 노드 서비스 계정의 `roles/container.defaultNodeServiceAccount` 바인딩을 완료 조건에서 제외하고 후속 처리로 둔다.
- 영향: 노드가 Cloud Logging/Monitoring에 로그·지표를 쓰지 못한다. 노드 Ready·스케줄링·PD 볼륨·`kubectl logs`는 영향이 없다.
- 후속: 프로젝트 Owner가 위 명령을 한 번 실행하면 된다(Terraform State 변경 없음). 이후 바인딩 재조회 결과를 이 표에 추가한다.

### 남은 제한 사항

- 실제 부분 실패·Worker 종료·State 잠금 경합은 장애 주입하지 않았다. 가짜 러너에서 실패·stale·재실행 금지를 검증했다. `UNKNOWN` 이후 자동 복구·자동 destroy는 없다.
- Plan 바이너리·State·인증 원문은 커밋하지 않는다. 현재 GKE는 11의 워크로드 검증을 위해 유지하고 있으며 비용이 발생한다.
- 구현·계약 변경은 `plan.md` §10과 이 티켓에 반영됐다. 제품 정책·외부 HTTP 계약 변경은 없다. G6 변경은 이 티켓과 `infra/terraform/gcp/README.md`에 반영했다.

> 2026-11-05 정리: 이 티켓에서 만든 검증 자원은 사용자 결정으로 삭제했다. 상세는 [티켓 14 검증 자원 정리](14-recovery-integration.md#검증-자원-정리-2026-11-05-사용자-결정).
