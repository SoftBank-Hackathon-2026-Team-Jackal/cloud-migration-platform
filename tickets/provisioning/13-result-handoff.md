# Ticket 13 — 실제 검증 결과와 Migration 인계

## 상태
DONE — [Issue #53](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/53); 착수 결정 R1~R10. 실제 클라우드 재실행 연결은 14에서 확인한다.

## 목적
배포 결과를 구조화해 Data/Traffic Migration이 필요한 연결 참조와 준비 상태를 소비하게 한다.

## 관련 요구사항
PROV-018, PROV-019, PROV-020, PROV-021

## 선행 작업
Blocked by: 12

## 작업 범위
- 포함: 인프라·워크로드·앱 검사 결과, `ProvisioningResult` 저장과 Worker 간 전달을 구현한다. 09·11·12의 실제 결과 refs 형식(AWS/GCP/K3s)을 모두 정규화하고 fixture로 검증한다(R9). 실제 클라우드 재실행은 14에서 확인한다.
- 제외: 데이터 복사·CDC 실행과 트래픽 실제 전환

## 수용 기준
- [x] DB Endpoint·인증 Secret 참조·앱 Endpoint·헬스 정보를 후속 단계에 전달한다.
- [x] 필수 검사 실패면 결과에 실패 단계·원인을 남기고 다음 단계 진행을 막는다.
- [x] 결과에 비밀번호 원문이 없고 Provisioning 완료를 데이터 이전 완료로 표시하지 않는다.

## 테스트 전략
- 제안 Test Seam: Provisioning Workflow 결과 → 후속 Worker 입력. 후속 Migration 자체의 성공은 확인하지 못한다.
- 실행: 준비/실패 결과 fixture와 Temporal 테스트 환경에서 후속 단계 인계·차단 확인; `make check`.

## 완료 시 확인할 동작
Data/Traffic Worker가 Secret 원문 없이 대상 접근 참조와 준비 상태를 읽는다.

## 관련 문서
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
- [요구사항](../../specs/provisioning/spec.md) · [구현 계획](../../specs/provisioning/plan.md) · [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 초안 기준 확인 필요: 결과의 외부 API 노출 범위와 기능 테스트 데이터 정책을 확정한다. → R7·R8로 결정.
- 구현 중 변경: 2026-11-05 착수 결정 R1~R10 참조.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록

## 착수 결정 (2026-11-05)

사용자와 합의한 인계 정보 정리(Data/Traffic Migration 담당자용)를 SDD에 적용한다. [Migration Network Preflight](../../specs/migration-network-preflight/spec.md)(#51)가 이 결과의 DB Endpoint·Agent 위치를 소비하므로 함께 맞춘다.

| ID | 결정 | 근거·대안 |
| --- | --- | --- |
| R1 계약 | `cmp_contracts.provisioning_result.ProvisioningResult` v1. 최상위: `contract_version`, `migration_id`, `plan_id`, `revision`, `approval_id`, `provider`, `scope`, `status`(`READY`·`INFRASTRUCTURE_READY`·`FAILED`·`UNKNOWN`), `code`, `data_state`(항상 `NOT_MIGRATED`), `cluster`, `namespace`, `finished_at`, `database`, `application`, `verification`, `failure`, `warnings`. 필드 의미는 아래 "인계 정보"에 둔다. | PROV-019 표, plan §11 제안 필드 |
| R2 상태 | 실행 `SUCCEEDED` + 워크로드 포함 scope(WORKLOADS/FULL) → `READY`. INFRASTRUCTURE scope 성공 → `INFRASTRUCTURE_READY`(DB·앱 없음, 인계 차단). 실행 `FAILED`/`UNKNOWN`은 그대로. 마이그레이션 완료를 뜻하는 값은 두지 않는다. | PROV-020 |
| R3 생성 위치 | `complete_approved_execution`이 실행 표식을 기록하는 같은 트랜잭션에서 순수 함수 `build_provisioning_result`(`cmp_providers.provisioning_result`)로 결과를 만들고 업무 DB `provisioning_results`(plan_id, revision 기본 키, 불변)에 저장한다. 입력은 저장된 Manifest, 승인 기록, 실행 상태·코드, 실패한 단계 이름, 정규화 전 refs뿐이다. 성공·실패·UNKNOWN 모두 저장하며, 실패 시 실제로 기록된 refs만 채운다. | plan §11 "성공·부분 실패 모두 저장" |
| R4 Provider refs 표준 키 | 결과에 필요한 비밀 아닌 값을 실행 refs에 추가한다. AWS/GCP 공통 기반: `db_version`, `db_storage_size_gib`, `app_port`, `app_health_path`, `app_replicas`. ON_PREM: READY일 때 `agent_id`, `cluster_uid`, `storage_class`, `db_host`, `db_port`, `db_name`, `db_user`, `db_version`, `db_storage_size_gib`, `db_secret_ref`, `db_release`, `app_release`, `app_port`, `app_health_path`, `app_replicas`, `app_internal_endpoint`(값은 승인 Job·Values에서). 기존 키는 유지한다. | 승인 Values가 단일 근거, 09·11·12 하위 호환 |
| R5 검증 목록 | 각 실행은 정해진 순서로 검사하고 첫 실패에서 멈춘다. 그래서 Provider별 검사 순서와 실패 코드 표로 `verification`을 만든다: READY면 전부 `PASSED`, 실패 시 해당 검사 `FAILED`(UNKNOWN이면 `UNKNOWN`), 앞선 검사 `PASSED`, 뒤 검사 `NOT_RUN`. 승인·Digest·산출물 같은 실행 전 확인 실패는 `failure.stage=PRECONDITION`, 검사 전부 `NOT_RUN`. `functional_tests`는 항상 `NOT_RUN`(`AFTER_DATA_MIGRATION`). | plan §11 세 층 검증, 실제 코드가 fail-fast |
| R6 실패 단계 | Workflow가 실패한 Activity 이름을 complete에 넘긴다(결정적 값). `execute_*_plan` → `INFRASTRUCTURE`, 워크로드 단계 → 배포 중 실패 `WORKLOAD_DEPLOY` / 배포 후 검사 실패 `POST_DEPLOY_VERIFICATION`. `recoverability`: FAILED → `RETRY_AFTER_FIX`, UNKNOWN → `CHECK_STATE_FIRST`, PRECONDITION → `RETRY_AFTER_FIX`. | PROV-019 "인프라 생성 실패와 생성 후 검증 실패 구분" |
| R7 Worker 인계 | Workflow 이력에는 기존처럼 `plan_id`·`revision`·`status`만 남긴다. 후속 Worker는 Activity `get_migration_handoff({plan_id, revision, consumer})`(`provisioning` queue)로 DB 결과를 읽는다. `consumer=DATA_MIGRATION`은 `common`+`database`, `TRAFFIC_MIGRATION`은 `common`+`application`과 진행 가능 여부(`proceed`, 차단 `blocked_by` 코드 목록)를 받는다. Data: `READY` + `database.ready`. Traffic: `READY` + `application.ready` + `external_endpoint` 존재. Data Migration의 동기화·정합성 완료는 Traffic 쪽이 별도로 확인하며 Provisioning이 판단하지 않는다. | 도메인 경계 §1 "필요한 만큼만", Temporal 흐름 §13 |
| R8 외부 API | 읽기 전용 `GET /provisioning/plans/{plan_id}/revisions/{revision}/result`. 인증은 기존 승인 조회와 같은 승인자 자격 증명·작업 권한. 노출: `status`·`code`, `provider`, 클러스터 종류·이름·위치, `data_state`, 앱 `external_endpoint`·`health_path`·`scheme`, `verification`, `failure`. 비노출: `database` 전체, 내부 Endpoint, Helm Release, Agent 식별자, 계정/프로젝트 ID. 결과가 없으면 404 `RESULT_NOT_FOUND`. | 사용자 확인·데모 필요, 내부 구성 최소 노출 |
| R9 범위 | 09(AWS)·11(GCP)·12(K3s)의 실제 refs 형식 fixture로 정규화를 검증한다. 실제 클라우드 재실행은 하지 않는다(14). 상위 `MigrationWorkflow` 연결은 범위 밖. | 09·11 완료로 형식 확정 |
| R10 기능 테스트 데이터 정책 | 배포 직후에는 데이터를 바꾸지 않는 검사(헬스, DB 인증 접속, 복제 권한)만 결과에 반영한다. 데이터 변경 기능 테스트는 정책 P-03에 따라 데이터 이전·정합성 확인 후, 트래픽 전환 전 단계에서 실행하며 실행 주체는 Data/Traffic 통합에서 정한다. | 정책 P-03 |

### 인계 정보

비밀값은 넣지 않는다: DB 비밀번호·`.env` 값·ECR 토큰·kubeconfig·Terraform State 내용·클라우드 자격 증명. Secret은 위치(namespace/name/key)만 둔다. 실패 시 생성되지 않은 Endpoint는 `null`이다.

```jsonc
{
  "contract_version": "1",
  "migration_id": "migration-…",      // 상위 마이그레이션 ID (Manifest)
  "plan_id": "plan-gcp-target",       // 배포 계획 ID
  "revision": 1,                      // 승인·실행한 Plan 버전
  "approval_id": "appr-…",            // 실행을 허가한 승인 기록
  "provider": "GCP",                  // AWS / GCP / ON_PREM
  "scope": "WORKLOADS",               // 승인 범위
  "status": "READY",                  // READY / INFRASTRUCTURE_READY / FAILED / UNKNOWN
  "code": "WORKLOADS_READY",          // 세부 코드(실패 시 원인 코드)
  "data_state": "NOT_MIGRATED",       // 항상 이전 전. 마이그레이션 완료 아님
  "cluster": {
    "kind": "GKE",                    // EKS / GKE / K3s
    "name": "cmp-target-gke",         // 클러스터 이름 (ON_PREM은 null 가능)
    "location": "asia-northeast3",    // 리전 또는 영역
    "account_id": "sbh26-prod",       // AWS 계정 / GCP 프로젝트 (API 비노출)
    "agent_id": null,                 // ON_PREM Agent (Preflight 실행 위치)
    "cluster_uid": null,              // ON_PREM 클러스터 UID
    "network_refs": {"vpc_network": "…"}  // 알려진 네트워크 참조만, 라우팅 CIDR은 Provisioning이 모름
  },
  "namespace": "cmp-demo-target",
  "finished_at": "2026-11-05T00:34:20+09:00",
  "database": {                       // Data Migration용
    "engine": "postgresql",
    "version": "16",
    "host": "cmp-demo-db.cmp-demo-target.svc",
    "port": 5432,
    "name": "demoapp",
    "user": "demoapp",
    "password_secret": {"namespace": "cmp-demo-target", "name": "cmp-demo-db-auth", "key": "password"},
    "replication_capable": true,      // 배포 직후 실측. 미확인이면 null + warnings
    "exposure": "CLUSTER_INTERNAL",   // 클러스터 내부 전용. 원본 접근 경로는 Data Migration이 준비
    "storage": {"pvc": "cmp-demo-db", "storage_class": "cmp-pd-balanced", "size_gib": 10},
    "release": "cmp-demo-db",
    "ready": true
  },
  "application": {                    // Traffic Migration용
    "release": "cmp-demo-api",
    "image": "…/cmp-demo-api@sha256:…",
    "internal_endpoint": "http://cmp-demo-api.cmp-demo-target.svc:8080",
    "external_endpoint": "http://34.102.136.6",  // 트래픽 목적지. 없으면 null
    "ingress": {"kind": "GKE_INGRESS", "name": "cmp-demo-target/cmp-demo-api", "address": "34.102.136.6"},
    "port": 8080,
    "health_path": "/health",
    "scheme": "http",                 // P0 HTTP만
    "tls": false,
    "replicas": 1,
    "ready": true
  },
  "verification": [                   // 검사별 결과 (R5)
    {"layer": "INFRASTRUCTURE", "name": "pvc_bound", "status": "PASSED", "code": null}
  ],
  "failure": null,                    // 실패 시 {stage, code, recoverability} (R6)
  "warnings": []                      // 예: REPLICATION_NOT_VERIFIED, EXTERNAL_ENDPOINT_UNAVAILABLE
}
```

### 합의한 Test Seam

- Seam: 승인 Workflow 완료(`complete_approved_execution`에 상태·코드·실패 단계·refs 전달) → 업무 DB `ProvisioningResult` → `get_migration_handoff` Activity와 결과 조회 API. SQLite 업무 DB(07 모델로 게시·승인), 09·11·12의 실제 refs 형식 fixture, Temporal 테스트 환경의 실제 `ProvisioningApprovalWorkflow`(가짜 Provider Activity)를 쓴다.
- 잡아내는 것: Provider별 정규화, READY/INFRASTRUCTURE_READY/FAILED/UNKNOWN 상태와 진행 차단, 실패 단계·검사 목록 추론, 결과·API·인계에 비밀번호 원문 없음, 마이그레이션 완료 미표시, 결과 불변(중복 complete), API 노출 범위·인증·404.
- 놓치는 것: 실제 클러스터 재실행(14), 상위 MigrationWorkflow 연결, Data/Traffic Worker 실제 소비.

## 구현 (2026-11-05)

- 계약: `cmp_contracts.provisioning_result`(R1). 변환·인계·요약: `cmp_providers.provisioning_result`의 순수 함수 `build_provisioning_result`·`handoff`·`public_summary`(R2·R5·R6·R7·R8).
- 저장: `cmp_api.provisioning_results`, 테이블 `provisioning_results`(Alembic `20261105_01`). `complete_execution`이 실행 표식과 같은 트랜잭션에서 한 번만 저장하고 이후 보고로 덮어쓰지 않는다(R3). 결과 생성이 실패해도 실행 표식 기록은 막지 않으며, 그때 인계는 `RESULT_NOT_FOUND`로 닫힌다.
- Workflow: `complete_approved_execution` 입력에 마지막으로 실행한 Provider Activity 이름(`step`)을 추가했다. 그 외 흐름·재시도·이력 내용은 바꾸지 않았다.
- Worker: Activity `get_migration_handoff`(`provisioning` queue) 등록.
- Provider refs(R4): AWS/GCP 공통 기반(`k8s_workloads`)과 ON_PREM(`onprem_rollout.resource_refs`·`values_refs`)에 표준 키를 추가했다. 기존 키는 유지한다.
- API(R8): `GET /provisioning/plans/{plan_id}/revisions/{revision}/result`, `make openapi`로 계약을 갱신했다.
- Network Preflight(#51) 연결: Preflight의 `probe_target.agent_id`·`cluster_uid`·`namespace`는 ON_PREM 결과의 `cluster.agent_id`·`cluster.cluster_uid`·`namespace`에서 읽는다. 원본 DB Endpoint와 라우팅 CIDR은 Provisioning이 알지 못하므로 결과에 없다(`network_refs`는 알려진 네트워크 이름만).

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_provisioning_result.py` | 14개 통과. 실제 `ProvisioningApprovalWorkflow`(Temporal 테스트 환경) → claim → 가짜 Provider(09·11·12 실제 refs 형식) → complete → DB 결과 → `get_migration_handoff`·API. GCP/AWS FULL/ON_PREM READY 정규화, 배포 후 검사 실패(PVC)·인프라 단계 유실(UNKNOWN)·사전 확인 실패·INFRASTRUCTURE scope의 단계·검사 목록과 진행 차단, 결과 없음, 알 수 없는 키·비밀값 제거, 결과 불변, 비정상 ref 값이 완료를 막지 않음, API 노출 범위·401/403/404 |
| `tests/test_gcp_workloads.py`·`tests/test_onprem_rollout.py` | R4 표준 키 생산 확인을 추가했고 통과 |
| `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` | Ruff 검사·포맷과 전체 510개 통과(Alembic 모델 일치 테스트 포함) |
| `make openapi` | 결과 조회 경로와 `ProvisioningResultSummary`·`Check`·`Failure` 스키마만 추가됐다. 기존 경로 변경 없음 |

### 미검증·제한

- `make smoke-temporal`은 로컬 Docker(Temporal)가 꺼져 있어 실행하지 못했다. Workflow 변경(complete 입력에 `step` 추가)은 Temporal 테스트 환경의 실제 Workflow 테스트로 확인했다.
- 실제 EKS/GKE/K3s 재실행으로 저장된 결과는 아직 없다. 09·11·12의 실측 refs 형식 fixture로만 정규화를 확인했고, 실제 연결은 14에서 확인한다.
- 검사 목록은 Provider의 고정 검사 순서와 첫 실패 코드에서 추론한 값이다. 실행이 개별 검사 결과를 직접 보고하지는 않는다. GCP `image_pull_secret`은 ECR 이미지가 없으면 실행하지 않는데도 READY에서는 `PASSED`로 표시된다.
- ON_PREM은 외부 Ingress가 없어 `external_endpoint`가 없다. 그래서 Traffic 인계는 `EXTERNAL_ENDPOINT_UNAVAILABLE`로 차단된다. 복제 권한도 확인하지 않아 `replication_capable=null`과 `REPLICATION_NOT_VERIFIED` 경고가 붙는다.
- 상위 `MigrationWorkflow`와 실제 Data/Traffic Worker의 소비는 범위 밖이다.

