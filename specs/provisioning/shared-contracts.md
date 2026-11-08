# 최소 공유 계약 — CP-01

## 구현 범위

[Issue #9](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/9)는 순차 개발에서 DB Chart, 클라우드 Plan 및 승인 게이트가 공유하는 내부 v1 모델과 순수 함수를 제공한다. HTTP/DB 승인 기록, Temporal 연결, S3 저장·권한 조회, Terraform/Helm 실행은 포함하지 않는다. 02부터 이 계약을 실제 산출물 생성에 연결한다. 문서 변경은 해당 구현 이슈/PR에 포함한다.

## 공개 모델·함수

| 경계 | 구현 위치 | 역할 |
| --- | --- | --- |
| `ArtifactRef`, `RenderedChart` | `cmp_contracts.artifact` | 불변 ID·버전·종류·바이트 크기·SHA-256 및 Chart/Values/Manifest 참조 |
| `PostgresChartValues` | `cmp_contracts.chart` | Sandbox/Production 공통 DB 입력. 환경·Provider·Release·Namespace·이미지·DB·Pod 자원·StorageClass/PVC |
| `DynamicBinding` | `cmp_contracts.chart` | 허용된 DB host/Secret 이름/URL Secret 이름 위치와 값 출처 |
| `DB_PROBE`, `judge_probe`, `default_parameters` | `cmp_providers.db_replication` | 대상 DB 논리 복제 기본 설정과 복제 가능 판정(27). AWS/GCP 배포 Activity와 ON_PREM Agent가 같은 SQL·판정을 쓴다 |
| `connection_url`, `connection_url_secret_name` | `cmp_providers.database_url` | DB 연결 URL 형식과 URL Secret 이름 규칙(19). Sandbox·운영 실행 주체가 함께 사용 |
| `PlanManifest`, `Preview`, `ApprovalTarget` | `cmp_contracts.plan` | Plan/Revision·템플릿 버전·대상·실행 범위·산출물 연결, 표시 요약, 승인 대상 식별 |
| `sha256_bytes`, `canonical_json_bytes`, `sha256_json`, `verify_artifact_bytes` | `cmp_providers.artifacts` | 순수 바이트/정규화 JSON Digest 및 조회한 실제 바이트 검증 |
| `approval_issues`, `approval_target`, `verify_approval_target` | `cmp_providers.plans` | 계획 완전성·Preview 일치·기록된 승인 대상 및 요청 실행 범위 비교 |
| `bind_values`, `verify_bound_values` | `cmp_providers.bindings` | 승인된 동적 자리만 채우고 나머지 Values 변경 거절 |
| `run_graceful`, `CommandResult.stopped`, `failed_apply_refs` | `cmp_providers.terraform_process` | Terraform 명령 시한·취소 시 SIGTERM → 유예 → SIGKILL, 중단 뒤 lock·보존 State 보고(53). `aws_apply.CommandResult`는 같은 클래스 |
| `plan_destroy`, `apply_destroy`, `destroy_addresses`, `review_digest` | `cmp_providers.cloud_destroy` | 실행된 Revision의 Spec으로 검토된 destroy. 검토 Digest = 환경·삭제 주소·GKE 보호 해제 여부(53) |

`cmp_*`는 기존 packages/contracts 또는 packages/providers의 Python 패키지다. 새 하위 패키지·런타임 의존성은 추가하지 않았다. 엄격한 Pydantic 모델로 알 수 없는 필드와 암묵적 타입 변환을 거절한다.

## ArtifactRef와 Digest

- `ArtifactRef = {id, version, kind, sha256, size_bytes}`. `kind`는 SPEC/TERRAFORM_PLAN/CHART/VALUES/BINDINGS/MANIFEST다. 원문·Secret·서명 URL은 참조에 넣지 않는다.
- 파일 Digest는 **저장된 정확한 바이트**의 SHA-256이다. 파일 읽기·접근 권한은 Activity에서 처리하고 받은 바이트를 `verify_artifact_bytes`로 검사한다. 이 함수는 크기와 Digest를 모두 확인한다.
- 구조화 모델의 Digest는 `model_dump(mode="json")` 결과를 `cmp-json-v1`으로 직렬화한다. 문자열 key 정렬, UTF-8, 공백 없는 구분자, 배열 순서 유지, Unicode 원문 유지 규칙이다. float/NaN/Infinity, 비문자열 key, tuple, bytes는 거절한다. 숫자는 정수와 명시적 단위 문자열을 사용한다. RFC 8785 전체 구현을 주장하지 않는다.
- Plan/Preview의 명시적 기본값·null도 Digest에 포함된다. 모델 변환 없이 외부 임의 JSON을 해시해 같은 Digest가 될 것이라고 가정하지 않는다.
- 불변 ID/버전의 실제 보장, 파일 내용 검증, 소유권 확인은 저장/조회 담당자가 수행한다. 올바른 형식의 참조가 있다는 사실만으로 파일 존재·권한·안전성을 증명하지 않는다.

## 계획과 Preview

PlanManifest에는 계약/Digest 버전, Migration ID, PlanRef(ID·Revision·템플릿 버전), 대상 식별, 실행 범위, 이미지 URI 및 산출물 참조가 있다. AWS/GCP는 계정 또는 프로젝트·리전·State 식별, ON_PREM은 Agent·기존 클러스터·Namespace를 고정한다. State는 같은 환경의 Revision마다 새로 만들지 않는다.

| scope | 승인 대상 구성을 위해 필요한 항목 |
| --- | --- |
| INFRASTRUCTURE | SPEC와 AWS/GCP 저장 TERRAFORM_PLAN. ON_PREM 클러스터 신규 구성은 P0 미지원 |
| WORKLOADS | SPEC, DB/App 각각 CHART·VALUES, BINDINGS, Digest 고정 앱 이미지 |
| FULL | WORKLOADS 전체 + AWS/GCP TERRAFORM_PLAN. ON_PREM은 Terraform Plan 없이 기존 K3s 사용 |

누락 상태의 Draft 모델을 읽을 수는 있지만 `approval_target`은 필수 항목 누락·오래된 Preview·검증 오류·미해결 결정·빈 변경 목록을 거절한다. 변경이 없으면 `NO_CHANGE` 항목을 명시한다. 경고는 표시와 Preview Digest에 포함되며 자동 실패로 취급하지 않는다.

Preview는 manifest Digest와 허용된 자원 변경 요약(종류·행위·이름·수량·크기), 적용 기본값, 오류·경고·미해결 결정을 갖는다. 대상 상세와 산출물은 연결된 manifest에서 읽는다. Terraform 원문을 Preview로 복사하지 않는다. 문자열 메타데이터의 민감성은 생성자가 검토해야 하며 이 모델이 임의 텍스트의 비밀값을 자동 탐지한다고 가정하지 않는다.

`ApprovalTarget`은 Plan ID·Revision·scope·manifest Digest·Preview Digest다. **승인 대상의 식별 정보이며 사용자 승인/권한의 증거가 아니다.** 07은 인증된 사용자·작업 권한·승인/거절 상태와 함께 이 값을 DB에 저장하고, 최신 활성 Plan·DB 승인 기록에서 읽은 값으로 실행 직전 비교해야 한다. 클라이언트가 보낸 ApprovalTarget만으로 실행하면 안 된다. 현재 구현은 DB 최신 Revision 조회, State drift 확인, 파일 재조회 또는 실제 실행을 수행하지 않는다.

실행 scope는 정확히 일치해야 한다. INFRASTRUCTURE 승인을 WORKLOADS/FULL 실행에 재사용할 수 없으며 FULL 승인의 일부만 실행하려는 경우도 현재 함수는 별도 범위 승인을 요구한다. 변경된 target 계정/State, Spec·Plan·Chart/Values Digest, 이미지·템플릿·Revision 또는 Preview는 기존 승인 대상과 일치하지 않는다. 저장소에서 같은 Revision을 덮어쓰지 않고 새 Revision으로 발급하는 책임은 Planning 저장 경계(18의 입력 Digest 판정)에 있다.

## DB Chart 입력과 동적 바인딩

`PostgresChartValues`는 SANDBOX/PRODUCTION 및 AWS/GCP/ON_PREM에서 같은 구조다. 클라우드 Credential을 받지 않는다. PostgreSQL 16의 이름·사용자, Secret 이름/키, Pod CPU millicores·메모리 MiB, PVC GiB·StorageClass를 명시한다. Node 디스크와 PVC를 혼동하지 않는다. 이미지 URI는 Digest에 고정한다. Registry 인증이 필요한 경우에만 참조를 제공한다.

선택 필드 `database.parameters`(27)는 Chart가 `postgres -c k=v` 인자로 넘기는 서버 설정의 허용 목록이다: `wal_level`(`replica`·`logical`), `max_replication_slots`·`max_wal_senders`(0~64), 표시용 `app`(`app.<키>`, 최대 8개, 키·값 문자 제한). 그 밖의 설정은 계약·스키마가 거절한다. 필드가 없으면 직렬화에서 빠져 이미 승인된 Values의 바이트·Digest와 렌더 결과가 그대로다. v2 공통 렌더러는 `wal_level=logical`·슬롯 10·송신자 10·`app.node_name=<provider 소문자>-<DB release>`를 기본으로 넣어, 새 Plan의 대상 DB가 역방향 복제 publisher가 될 수 있다.

이름은 초기 구현에서 보수적인 Kubernetes label 형태, Secret key는 영숫자·점·밑줄·하이픈으로 제한한다. 이미지가 실제 PostgreSQL 16인지, Secret/PVC가 존재하는지는 렌더/실환경 검증에서 확인한다. DB Chart의 Helm 템플릿은 02에서 `infra/helm/postgres`에 구현했다. 사용법·렌더 제한은 [Helm 안내](../../infra/helm/README.md)를 따른다. 현재 Chart는 해결되지 않은 `registry_auth_ref`를 거절하며, 실제 Secret/StorageClass 존재·이미지 실행 검증은 후속 배포 경계다.

동적 바인딩은 `database.host ← DATABASE_HOST`, `database.secret_name ← DATABASE_SECRET_NAME`, `database.url_secret_name ← DATABASE_URL_SECRET_NAME`(19)만 지원한다. 마지막 자리는 선택 항목이라 미설정 시 직렬화에서 빠지며(이미 승인된 Values 바이트 유지), 앱이 `connection_url_secret_ref`를 쓸 때만 규칙이 생긴다. 이는 런타임 DB 연결을 쓰는 Values 템플릿의 자리이며 DB Chart에 host 필드를 추가한다는 뜻은 아니다. 대상 자리가 미리 존재하고 null이어야 한다. 비밀번호 원문 대신 생성된 Kubernetes Secret **이름**을 전달한다. 제공한 source key가 빠지거나 남으면, 경로가 중복되거나 source/target이 어긋나면 실패한다. 원본 템플릿은 변경하지 않는다.

`verify_bound_values`는 실제 Values가 승인 템플릿에 승인된 참조만 채운 값과 정확히 일치하는지 검사한다. 이미지/Replica/임의 필드를 바꾸거나 주입된 DB 참조가 실제 결과와 달라지면 실패한다. 템플릿·바인딩 규칙의 실제 바이트는 호출 전에 승인된 ArtifactRef로 검증해야 한다.

## 재현

Fixture는 저장소 기준 `tests/fixtures/contracts/`의 `aws-plan.json`, `gcp-plan.json`, `on_prem-plan.json`, `postgres-values.json`이다. 예제 ID·이미지·Digest는 테스트 데이터이며 실제 클라우드 산출물이 아니다.

```python
from cmp_contracts.plan import PlanManifest, Preview
from cmp_providers.plans import approval_target, manifest_digest

# payload는 위 fixture 또는 신뢰 가능한 Planning 결과에서 읽은 내부 v1 dict
manifest = PlanManifest.model_validate(payload)
preview = Preview(
    contract_version="1",
    manifest_digest=manifest_digest(manifest),
    changes=[{"resource_type": "CLUSTER", "action": "CREATE", "name": "demo"}],
)
identity = approval_target(manifest, preview)
# identity는 사용자가 승인할 대상이다. 이 호출 자체가 사용자 승인을 만들지 않는다.
```

`uv run pytest tests/test_artifacts.py tests/test_chart_contracts.py tests/test_plan_contracts.py tests/test_bindings.py`로 공개 경계 테스트를 재현한다. 후속 기능이 완성되기 전에는 fixture 통과를 실제 저장 Plan·Helm 렌더·승인 실행의 성공으로 표시하지 않는다.

## 앱 렌더 소비자 (03)

`cmp_contracts.application`과 `cmp_providers.application`이 기존 DynamicBinding을 소비한다. `cmp_providers.app_renderer.render_application`은 검증된 참조 전용 Values로 실제 Helm Manifest bytes를 만든다. [상세 계약](application-rendering.md)을 따른다. 승인/저장/실배포 기능을 대신하지 않는다.

## Sandbox/온프레미스 조합 소비자 (04A)

기존 `PostgresChartValues`, `ApplicationValues`, DB 바인딩과 두 Helm Chart를 조합하는 [공통 배포 입력·렌더 결과 계약](onprem-shared-deployment.md)을 04A에서 구현한다. SANDBOX/PRODUCTION은 같은 입력 형태를 쓰고 Namespace·StorageClass·Secret 참조 등 환경별 값을 다르게 전달한다. 순수 렌더 결과는 K3s 적용 성공·Ready 또는 Sandbox 기능 검증을 뜻하지 않는다.

## ID 기반 다중 리소스 소비자 (04B)

`cmp_contracts.shared_deployment_v2.SharedDeploymentRequestV2`와 `cmp_providers.shared_deployment_v2.render_shared_deployment_v2`는 여러 DB/App 입력을 고유 ID로 렌더한다. DB별 기존 `PostgresChartValues`와 앱별 `ApplicationValuesV2`를 같은 Chart에 공급해 Values·Manifest·DB 출력·해결된 환경변수 참조를 반환한다. 입력에서 Secret 원문과 미지원 종류/확장을 거절한다. Worker는 환경별 리소스 값·Secret·대상 capabilities를 준비하고 Chart 산출물을 저장·승인 내용과 대조한 뒤 실제 적용한다. v1 조합 호출은 그대로 유지한다.

## 승인 게이트 소비자 (07)

`cmp_api.approvals`가 `approval_issues`·`approval_target`·`verify_approval_target`을 그대로 사용한다. 승인 요청 시 업무 DB의 최신 Revision에서 읽은 manifest/preview로 대상 식별을 계산하고 요청 본문의 scope·Digest와 비교한다. 실행 직전에는 DB 승인 기록의 대상 식별과 저장된 현재 manifest/preview를 `verify_approval_target`으로 다시 비교한다. `LOCAL_STATE_VALIDATION_ONLY`(AWS, pending_decisions)와 `LOCAL_STATE_UNAPPROVABLE`(GCP, errors)은 기존 `PENDING_DECISION`/`VALIDATION_FAILED` 규칙으로 승인 불가다. 공통 모델·함수는 복제하거나 변경하지 않았다. 상세는 [07 티켓](../../tickets/provisioning/07-approval-gate.md)을 따른다.

## 승인된 ON_PREM 배포 소비자 (12)

`cmp_api.onprem_rollout`이 07의 승인 기록·`latest_revision`·`manifest_digest`와 `verify_artifact_bytes`를 재사용해 Agent 작업을 만들고, 04A의 `agent_jobs`·인증·결과 보고와 04B의 Values 모델을 그대로 쓴다. 새 공통 모델은 Agent 작업 본문 `cmp_contracts.onprem_rollout.ProductionRolloutJob`뿐이다. 실행 dispatch는 08 소유 계약을 따른다. 52(#187)가 작업 본문을 바꾸지 않고 `agent_jobs`에 시한(`deadline_at`)·취소 요청(`cancel_requested_at`) 열과 상태 `EXPIRED`·`CANCELLED`를 더했다(상태 조회 `state`도 같은 두 값). claim 응답에는 선택 필드 `expires_in_seconds`가 붙는다. 새 작업 본문 `cmp_contracts.onprem_teardown.TeardownJob`(`ENVIRONMENT_TEARDOWN`)과 관리자 경로 `GET /agent-admin/jobs/{job_id}`, `POST .../cancel`, `POST .../teardown`이 생겼다. 상세는 [공통 배포 계약](onprem-shared-deployment.md#승인된-운영-배포-12-36)을 따른다.

## 온프레미스 Preview 생성 (12A)

15(#69)는 새 공유 계약을 만들지 않는다. 04A 등록 계약의 `cluster_uid`를 발급 시 선택으로 바꾸고 Agent 마지막 접속·폐기 상태를 추가했다. 상세는 [공통 배포 계약 §상시 실행·설치·폐기](onprem-shared-deployment.md#상시-실행설치폐기-15-69)를 따른다.

`cmp_providers.onprem_preview.onprem_preview(manifest, contents)`는 ON_PREM WORKLOADS/FULL 계획이 고정한 `database_values`·`application_values` 바이트를 `verify_artifact_bytes`로 확인하고 공통 `Preview`를 만든다. 보여 주는 내용은 기존 K3s·Agent·Namespace, DB StatefulSet·PVC, 앱 Deployment·Service, 외부 공개 없음, 적용 기본값(Namespace 생성, DB 비밀번호 Secret 생성)이다. 클러스터를 조회하지 않으므로 경고 `CLUSTER_STATE_NOT_INSPECTED`를 붙인다. 사용자 Secret은 이름·키만 `USER_SECRET_REQUIRED` 경고로 표시하고 환경변수·Secret 값은 넣지 않는다. Values의 대상·이미지가 manifest와 다르면 `errors`에 `VALUES_TARGET_MISMATCH`/`IMAGE_MISMATCH`를 넣어 `approval_issues`가 승인을 거절하게 한다. Digest 불일치·비 ON_PREM은 `ProvisioningInputError`로 Preview를 만들지 않는다. Planning 저장 경계와의 연결은 18이 구현했다(아래 §18). 상세는 [12A 티켓](../../tickets/provisioning/12a-onprem-preview.md)을 따른다.

## Provisioning 결과 인계 (13)

`cmp_contracts.provisioning_result.ProvisioningResult` v1이 Provisioning → Data/Traffic Migration 인계 계약이다. `cmp_providers.provisioning_result`의 `build_provisioning_result`(실행 상태·코드·단계·refs → 결과), `handoff`(소비자별 뷰·`proceed`/`blocked_by`), `public_summary`(사용자 요약)는 순수 함수다. 저장·조회는 `cmp_api.provisioning_results`(`add_result`/`load_result`, 테이블 `provisioning_results`), Worker 진입점은 Activity `get_migration_handoff`, 사용자 조회는 `GET /provisioning/plans/{plan_id}/revisions/{revision}/result`다. 필드·판정 규칙은 [티켓 13 결정 R1~R10](../../tickets/provisioning/13-result-handoff.md#착수-결정-2026-11-05)을 따른다. Provider 실행 refs의 표준 키(R4)는 이 결과의 입력이며 기존 키는 유지한다.

**인계 조회 방식 (50, #185):** 이름·코드는 `cmp_contracts.handoff`에 있다. 두 Activity 모두 Provisioning 큐에서 저장된 결과를 Plan Revision(AWS/GCP는 **워크로드** Revision)으로 읽고, 조회 시점에 ON_PREM 터널 상태를 합친다.

| Activity | 입력 | 출력 | 소비자 |
| --- | --- | --- | --- |
| `get_migration_handoff` | `{plan_id, revision, consumer}` | R7 뷰 `{plan_id, revision, consumer, common, database\|application, proceed, blocked_by}` | 데이터 이전(`cmp_data.handoff.target_endpoint`, DATA_MIGRATION) |
| `get_traffic_handover` | `TrafficHandoverRequest{plan_id, revision}` | `TrafficHandoff{contract_version:"1", plan_id, revision, provider, proceed, blocked_by, handover}` | 상위 Workflow → `TrafficShiftWorkflow` 요청의 `handover`(#128) |

`handover`(`TrafficHandover`)는 Traffic `cmp_traffic.contracts.Handover`와 필드·기본값이 같다(계약 테스트가 실제 모델로 고정). 변환 `cmp_providers.provisioning_result.traffic_handover(view)`:

| Handover | 인계 뷰(TRAFFIC_MIGRATION) |
| --- | --- |
| `status` | `common.status`: READY→READY, FAILED→FAILED, INFRASTRUCTURE_READY·UNKNOWN·결과 없음→UNKNOWN |
| `external_endpoint` | `http://{application.external_host}:{application.external_port}`(둘 다 있을 때만) |
| `port` | `application.external_port`(AWS/GCP LB 80, ON_PREM 터널 쪽 앱 포트). 앱 `port`(클러스터 안 컨테이너 포트)는 쓰지 않는다 |
| `health_path` | `application.health_path`(= Plan 앱 `health_path`, 분석이 readinessProbe > livenessProbe > 헬스 이름 코드 라우트 > `/` 순으로 정함, [64](../../tickets/provisioning/64-health-path-from-analysis.md)), 결과에 없을 때만 `/health` |
| `ready` | `proceed` ∧ status READY ∧ 목적지 있음 ∧ `application.ready` ∧ HTTP·TLS 없음(P0). `TrafficHandoff.proceed`와 같다 |

막힘 코드(`blocked_by`, 첫 값이 원인): `RESULT_NOT_FOUND`, `WORKLOADS_NOT_DEPLOYED`, `PROVISIONING_NOT_READY`, `DATABASE_NOT_READY`, `TUNNEL_DB_NOT_VERIFIED`, `APPLICATION_NOT_READY`, `EXTERNAL_ENDPOINT_UNAVAILABLE`, `TUNNEL_STATUS_STALE`. 조회 자체가 실패하면 Workflow가 `HANDOFF_UNAVAILABLE`로 기록한다. 데이터 이전이 읽는 DATA_MIGRATION 필드는 `proceed`, `blocked_by`, `common.namespace`, `common.cluster.{kind,name,location,account_id}`, `database.{release,name,tunnel_endpoint}`이며 이 티켓에서 바꾸지 않았다. AWS/GCP DB 외부 주소는 두지 않는다(CDC는 `kubectl exec`, 클러스터 사이 주소는 WireGuard `peer_host` 입력). 상세는 [50 H1~H8](../../tickets/provisioning/50-handoff-contract.md#착수-결정-2026-11-06).

**ON_PREM 트래픽 인계 대기 (67, #284):** `get_traffic_handover`는 ON_PREM 뷰가 `EXTERNAL_ENDPOINT_UNAVAILABLE`·`TUNNEL_STATUS_STALE`로만 막혔으면(`tunnel_wait_needed`) Activity 안에서 `TRAFFIC_HANDOVER_POLL_SECONDS`(기본 2초)마다 다시 읽고, **저장된 결과의 `finished_at` 이후 보고된** 터널 상태가 앱 헬스 통과·stale 아님이 되는 순간 진행한다. 롤아웃 이전 보고는 뷰가 통과해도 `TUNNEL_STATUS_STALE`로 막는다(`after_rollout`, 터널 뷰에 `reported_at`). 상한 `TRAFFIC_HANDOVER_WAIT_SECONDS`(기본 180초, 540초로 자름)까지 안 풀리면 마지막 뷰의 코드 그대로 돌려준다. 다른 막힘·결과 없음·AWS/GCP는 바로 돌려준다. 대기 중 heartbeat를 보내고, `MigrationWorkflow`는 `traffic-handover-wait-v1` patch 아래 이 Activity를 `start_to_close` 600초·`heartbeat` 30초로 예약한다. `get_migration_handoff`(데이터 인계)는 바꾸지 않았다. 상세는 [67 D1~D9](../../tickets/provisioning/67-handover-tunnel-wait.md#착수-결정-2026-11-07-issue-284).

**ON_PREM 데이터 인계 대기 (80, #319):** `get_migration_handoff`(consumer DATA_MIGRATION)도 ON_PREM 뷰가 `TUNNEL_DB_NOT_VERIFIED`·`TUNNEL_STATUS_STALE`로만 막혔으면(`tunnel_db_wait_needed`) 같은 설정(`TRAFFIC_HANDOVER_POLL_SECONDS`·`TRAFFIC_HANDOVER_WAIT_SECONDS`)으로 Activity 안에서 기다리고, 구축 이후 보고만 근거로 삼는다(`data_after_rollout`, 이전 보고면 `TUNNEL_STATUS_STALE`·`tunnel_endpoint: null`). 상한까지 안 풀리면 마지막 코드 그대로. 대기 중 `provision.check{name:"tunnel_db"}`(phase `data_copy`)와 heartbeat를 내고, `MigrationWorkflow`는 `data-handoff-wait-v1` patch 아래 600초·heartbeat 30초로 예약한다. 반환 필드는 바뀌지 않는다. 상세는 [80 D1~D7](../../tickets/provisioning/80-onprem-data-handoff-wait.md#착수-결정-2026-11-07-issue-319).

## 터널 설정 교환 (16)

`cmp_api.tunnels`와 `cmp_api.routes.tunnels`가 Agent 공개키 등록(응답에 중계 서버 목록 포함)·터널 IP 배정·Agent 터널 설정 조회·터널 상태 보고·중계 서버 등록·중계 서버용 Agent 목록(`GET /relays/{relay_id}/agents`, 중계 서버 자격 증명, 폐기 Agent는 `revoked: true`로 남음, 내용 digest인 `list_version`)을 제공한다. 결과 계약에는 `DatabaseHandoff.tunnel_endpoint`와 `ApplicationHandoff.external_host`·`external_port`가 추가됐다. `external_endpoint`는 "중계 서버가 전달할 목적지"이며 호환을 위해 유지하고, 같은 목적지를 host·port로도 준다(AWS/GCP는 LB 주소·80). `get_migration_handoff`는 ON_PREM 결과에 최신 터널 상태를 읽는 시점에 합친다. Data 인계는 터널 경유 DB 접속 확인 뒤, Traffic 인계는 터널 경유 앱 헬스 확인 뒤(`http://터널IP:앱포트`)에만 진행한다. 중계 서버 공인 주소는 어느 인계 필드에도 없다. 상세는 [티켓 16 T1~T13](../../tickets/provisioning/16-tunnel-config-exchange.md)을 따른다.

**터널 상태 만료 (25, #135):** 터널 보고는 `now - reported_at <= TUNNEL_STATUS_TTL_SECONDS`(기본 60초 = 15초 보고 4회 누락)이고 `last_handshake_at`이 `TUNNEL_HANDSHAKE_STALE_SECONDS`(기본 180초, WireGuard Reject-After-Time) 이내일 때만 현재 근거다(API 서버 시계, 읽는 시점마다 판정). 확인 항목이 보고상 성공이어도 보고가 만료되면 ON_PREM 인계 뷰는 `tunnel_endpoint`·`external_*`를 채우지 않고 `blocked_by: ["TUNNEL_STATUS_STALE"]`(Data·Traffic 공통)로 막는다. 보고 없음·보고상 실패는 기존 코드(`TUNNEL_DB_NOT_VERIFIED`, `EXTERNAL_ENDPOINT_UNAVAILABLE`) 그대로다. 새 보고가 오면 다음 조회에서 다시 진행된다. 저장된 결과는 바뀌지 않고 AWS/GCP는 영향이 없다. 상세는 [티켓 25 F1~F7](../../tickets/provisioning/25-tunnel-status-freshness.md)을 따른다.

## Plan 생성·게시와 Preview 조회 (18)

새 공유 모델은 `cmp_contracts.plan_publish`의 `PlanningInputRegistration`(Planning 입력 v2 + Provider별 대상 설정 + 선택 요구사항 바이트)과 `plan_id_for`(Migration당 Plan ID)뿐이다. 기존 `PlanManifest`·`Preview`·`approval_target`·`onprem_preview`·05/06 Plan 함수는 바꾸지 않고 호출한다. `publish_plan_revision`은 선택 인자 `input_digest`를 받아 같은 트랜잭션에 `provisioning_plan_publications`를 기록한다(기존 호출 동작은 그대로). `ProvisioningApprovalWorkflow` 요청은 선택 필드 `approval_timeout_seconds`를 받아 승인 없이 기간이 지나면 `EXPIRED/APPROVAL_EXPIRED`로 끝난다. 상세는 [18 티켓](../../tickets/provisioning/18-onprem-plan-publish.md#착수-결정-2026-11-05)을 따른다.

## AWS/GCP 워크로드 Plan 게시 (22)

22(#108)는 공유 모델에 하위 호환 필드만 더한다. `AwsTargetSettings`·`GcpTargetSettings`의 선택 필드 `workloads: CloudWorkloadSettings{namespace, storage_class, database_image}`와 `workload_plan_id_for`(Migration당 워크로드 Plan ID)다. 18 입력 Digest는 `workloads`를 빼고 계산해 기존 등록의 Digest가 바뀌지 않는다. 인프라 Revision 실행 결과(업무 DB refs)로 별도 `WORKLOADS` Plan을 게시하며, manifest `target`은 인프라 manifest와 같아 09·11이 그 인프라 실행의 클러스터를 찾는다. 앱 Values는 09·11의 v1 `ApplicationValues` 템플릿이고 산출물은 `PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/<sha256>`에 둔다. Preview는 `cmp_providers.cloud_workload_preview`(순수 함수). `PlanManifest`·`Preview`·`ProvisioningResult`·승인·실행 계약은 바꾸지 않는다. 상세는 [22 티켓](../../tickets/provisioning/22-cloud-workload-plan.md#착수-결정-2026-11-05).

## 운영 NetworkPolicy (26)

추가 공개 모듈 `cmp_providers.network_policy`: `base_policies`·`entry`(샌드박스 공용), `production_policies`·`with_connections`·`describe`, `aws_vpc_cni_enforces`·`gke_dataplane_v2`, 상수 `PRODUCTION_LABELS`·`TEMPLATE_VERSION`. 계약 추가(모두 선택, 없으면 이전 바이트 그대로): `ArtifactRole` `network_policies`(kind `MANIFEST`, 내용 `{"policies": [NetworkPolicy...]}`), `ProductionRolloutJob.network_policies: ArtifactRef`(Agent는 `artifact_refs()`로 받음). 입력 Digest에 `network_policy` 표식(ON_PREM 18, AWS/GCP 22 워크로드). 09·11 결과 refs `network_policy_enforcement`·`network_policies`, 실패 코드 `NETWORK_POLICY_NOT_ENFORCED`·`NETWORK_POLICY_CONFLICT`·`INVALID_NETWORK_POLICIES`(Agent는 `NETWORK_POLICY_MISSING`도). `ClusterSession`에 `get_object`·`network_policies`·`delete_network_policy`. 상세는 [26](../../tickets/provisioning/26-production-network-policy.md#착수-결정-2026-11-06).

## 환경 연결 (23)

[ADR-005](../../docs/decisions/ADR-005.md), [23 티켓](../../tickets/provisioning/23-environment-connections.md). 모든 라우트는 운영자 키 `X-Enrollment-Admin-Key`가 필요하고, 웹 BFF가 서버 측에서만 호출한다(CORS 없음). 시각은 epoch 초 정수다. 422 응답에는 입력값이 없다.

| 라우트 | 요청 | 응답 |
| --- | --- | --- |
| `POST /connections` | `{kind: AWS_SOURCE\|AWS_TARGET\|GCP_TARGET, display_name, region, gcp?: {project_id, expected_principal}}`. `gcp`는 GCP_TARGET에만, 다른 필드는 거절한다 | 201 `{connection_id, kind, display_name, status: "PENDING", external_id, launch_stack_url}`. GCP는 둘 다 `null`, 템플릿 호스팅이 없으면 링크 `null` |
| `PUT /connections/{id}/role` | `{role_arn}`(`arn:aws[-cn\|-us-gov]:iam::<12자리>:role/...`) | 상세. 한 번만 등록(같은 값 멱등, 다른 값 409 `ROLE_ALREADY_SET`, GCP 409 `ROLE_NOT_APPLICABLE`) |
| `POST /connections/{id}/verify` | — | 200 `{status: CONNECTED\|FAILED, code, checked_at, account_id\|project_id, principal, eks_clusters?}`. `eks_clusters`는 성공한 AWS_SOURCE에만 있다. 코드: `OK`, `ROLE_NOT_SET`, `ASSUME_ROLE_DENIED`, `ASSUME_ROLE_FAILED`, `PLATFORM_CREDENTIALS_UNAVAILABLE`, `IDENTITY_CHECK_FAILED`, `ACCOUNT_MISMATCH`, `EKS_LIST_DENIED`, `EKS_LIST_FAILED`, `ADC_*` |
| `GET /connections` | — | `{"connections":[{connection_id, kind, display_name, account_id\|project_id, region, status, last_verified_at}], "agents":[Agent 항목]}`. External ID는 없다 |
| `GET /connections/{id}` | — | 상세: 위 필드 + `role_arn`, `external_id`, `expected_principal`, `principal`, `credential_ref`, `launch_stack_url`, `stack_name`, `last_code`, `last_checked_at` |
| `GET /connections/{id}/eks-clusters?region=` | 리전(생략 시 연결 리전) | `{connection_id, region, eks_clusters: [이름]}`. AWS_SOURCE 전용, 실패는 409 + 코드 |
| `DELETE /connections/{id}` | — | `{connection_id, deleted: true, stack_name, notice}`. 플랫폼 데이터·생성 프로필만 지운다. 사용자 계정 스택은 사용자가 지운다 |
| `GET /agent-admin/agents` | — | `{"agents":[Agent 항목]}` |

Agent 항목:

```json
{"agent_id": "...", "cluster_uid": "...", "last_seen_at": 0, "online": true, "revoked": false, "version": null,
 "tunnel": {"tunnel_ip": "10.200.0.2", "last_handshake_at": 0, "app_health_ok": true, "db_connect_ok": true,
            "stale": false, "fresh_until": 60}}
```

- `online`: 폐기되지 않았고 `last_seen_at`이 60초 이내다.
- `tunnel`: 16의 마지막 `agent_tunnel_status` 보고다. 보고가 없으면 `null`이다.
- `tunnel.stale`·`tunnel.fresh_until`(25 F6, 추가 필드): 보고가 위 만료 규칙상 현재 근거가 아니면 `stale: true`다. `fresh_until`은 `min(reported_at + 60, last_handshake_at + 180)`(설정값 기준), handshake가 없으면 `null`이고 `stale: true`다. 화면은 `stale`이면 앱/DB OK를 "확인 만료"로 보인다. `online`은 위 정의 그대로다.
- `version`: Agent 버전 보고가 후속이라 `null`이다.

**`credential_ref` 해석:** 역할이 등록된 AWS 연결의 `credential_ref`는 `cmp-conn-<연결 hex>`다. 이 이름은 생성된 AWS config 파일(`connection_aws_config_file`)의 프로필이다. 프로필은 `role_arn`, `external_id`, `source_profile = connection_platform_aws_profile`, `role_session_name`, `duration_seconds = 3600`, `region`이다. 분석 `cmp_scanners.aws_infra.get_session(credential_ref)`과 Provisioning `aws_profile`에 그대로 넘긴다. 프로세스는 `AWS_CONFIG_FILE`이 이 파일을 가리켜야 한다. 대기 중인 연결과 GCP 연결은 `credential_ref: null`이다. GCP 데모는 기존 `provisioning_gcloud_configuration`·ADC 설정을 쓴다.

API 설정(`cmp_api.config`):
- `connection_platform_aws_profile`: verify가 쓰는 플랫폼 주체 프로필이자 `source_profile`
- `connection_platform_principal_arn`: 템플릿 신뢰 대상
- `connection_template_base_url`: S3 템플릿 접두어
- `connection_aws_config_file`, `connection_aws_base_config_file`: API가 시작·역할 등록·삭제 때 다시 쓴다(46)
- `connection_gcp_project_id`
- `platform_ecr_aws_profile`(46): Agent ECR 토큰 발급 주체. 없으면 `connection_platform_aws_profile`(41 동작)

**Provisioning의 연결 사용 (46):** AWS Plan 입력 `credential_ref`(`VersionRef`)의 `id`가 연결 프로필 이름 `cmp-conn-<12 hex>`(44의 시작 요청이 `PlanningSettings.target.settings.credential_ref`에 넣는 값) 또는 연결 ID `conn-<12 hex>`면 대상 연결이고, `version`은 `"1"`이다(Role ARN은 한 번만 등록). 새 계약 필드는 없다. 두 접두어로 시작하지만 형식이 틀리면 `INVALID_CREDENTIAL_REF`, 그 밖의 값이면 worker 고정 프로필(이전 동작).
- `cmp_providers.aws_credentials`: `connection_profile(id)`, `connection_id_of(ref)`, `resolve_aws_profiles(ref, *, platform_profile) -> AwsProfiles(provider, state, connection_id)`. `plan_aws_infrastructure`·`apply_aws_infrastructure`의 `aws_profile`은 플랫폼 프로필(State backend·기본값) 뜻이다. S3 backend `init`에 `-backend-config=profile=<state>`가 항상 붙는다.
- `cmp_api.connections.check_target_connection(session, id, *, account_id)`: Plan 게시 전 검사. 실패 `PLAN_FAILED` + `TARGET_CONNECTION_NOT_FOUND`·`TARGET_CONNECTION_KIND_MISMATCH`·`TARGET_CONNECTION_NOT_VERIFIED`·`TARGET_CONNECTION_ACCOUNT_MISMATCH`.
- 연결 경로 Plan: `cluster_name`이 `cmp-`로 시작해야 한다(`INVALID_CLUSTER_NAME`).
- AWS Plan 명령 실패 issues: 첫 코드(계정 확인 단계는 `AWS_IDENTITY_UNAVAILABLE`, 그 밖은 `PLAN_COMMAND_FAILED`) + 단계 코드 `PLAN_STEP_IDENTITY|INIT|VALIDATE|PLAN|SHOW`(경로 `terraform.<단계>`).
- AWS 워크로드: `ApprovedWorkloads.runtime_for(approved)` 훅, `Approved.spec`(승인된 SPEC 문서), `CliRuntime.with_profile`. 응답 추가: `POST /connections`·`GET /connections/{id}`의 `platform_principal_arn`(AWS만, 설정 없으면 `null`).

## 이전 범위 선택 (56)

[티켓 56](../../tickets/provisioning/56-scope-selection.md#착수-결정-2026-11-06-issue-219-표). 23과 같은 라우터(운영자 키, detail은 코드만, 422에 입력값 없음). 모두 확인된(`CONNECTED`) `AWS_SOURCE` 연결이 필요하다. 쿠버네티스 읽기는 클러스터별 접근 항목(`infra/connections/eks-view-access.yaml`, 호스팅 `connections/v2/eks-view-access.yaml`)이 있어야 한다.

| 라우트 | 요청 | 응답 |
| --- | --- | --- |
| `GET /connections/{id}/eks-clusters/{cluster}/namespaces?region=` | 리전(생략 시 연결 리전) | `{connection_id, region, cluster, namespaces: [이름]}`. `kube-*`·`amazon-*`·`aws-*`·`*-system` 제외 |
| `GET /connections/{id}/eks-clusters/{cluster}/namespaces/{namespace}/workloads?region=` | 〃 | `{connection_id, region, cluster, namespace, workloads: [{kind: Deployment\|StatefulSet, name}]}` |
| `POST /connections/{id}/scope-analyses` | `{region, cluster, namespace, workload, workload_kind?}` | `ScopeAnalysis {analysis_id: "sa-…", selection: {connection_id, region, cluster, namespace, workload, workload_kind}, candidates: [ScopeCandidate]}` |
| `POST /scope-analyses/{analysis_id}/confirm` | `{included: [candidate_id]}` | `ScopeConfirmation {scope_id: "scope-…", analysis_id, included, excluded, confirmed_at}`. `confirmed_at`은 ISO-8601 UTC 문자열 |

`ScopeCandidate`: `{candidate_id, resource_type, name, arn|null, evidence: [{kind, location}], suggested}`. `resource_type`: `ALB`, `IAM_ROLE`, `S3`·`SQS`·`SNS`·`DYNAMODB`·`RDS`·`ELASTICACHE`·`SECRETS_MANAGER`·`KMS`·…(IAM 정책 리소스 서비스), `SECRET`, `CONFIG_MAP`, `POSTGRESQL`. `evidence.kind`는 분석 `Evidence.kind` 값 중 이 API가 실제로 읽는 `MANIFEST`·`CLOUD_RESOURCE`·`IAM_POLICY`·`CONFIG`만 쓴다.

오류: `CONNECTION_NOT_FOUND` 404, `SOURCE_CONNECTION_REQUIRED`·`CONNECTION_NOT_READY`·`EKS_ACCESS_DENIED`·`EKS_API_FAILED`·`ASSUME_ROLE_DENIED`·`ASSUME_ROLE_FAILED`·`PLATFORM_CREDENTIALS_UNAVAILABLE` 409, `CLUSTER_NOT_FOUND`·`NAMESPACE_NOT_FOUND`·`WORKLOAD_NOT_FOUND`·`ANALYSIS_NOT_FOUND` 404, `INVALID_SCOPE` 400(빈 `included` 또는 분석에 없는 id).

공개 모듈: `cmp_api.scope_selection`(`eks_token`, `EksKubeApi`, `KubeApi`, `KubeNotFound`, `open_cluster`, `list_namespaces`, `list_workloads`, `analyze_scope`, `confirm_scope`, `ScopeAnalysisRecord`, `ScopeConfirmationRecord`, `REQUIRED_AWS_ACTIONS`). 라우트 의존성 `get_kube_factory`(테스트 교체용). 분석·worker는 아직 확정 범위를 읽지 않는다(후속).

## 이전 이벤트 저장소·SSE (24)

`cmp_contracts.events`가 이벤트 봉투 `MigrationEvent`와 웹 계약(`cloud-migration-web` `contract/events.schema.json`, 사본 `docs/contracts/events.schema.json`)과 같은 타입별 `*Data` 모델, 백엔드→웹 이름 변환 `to_web`, 웹 형식 스키마 `event_json_schema()`를 제공한다. 저장은 `cmp_api.migration_events`(`migration_events`·`migration_logs`, `append_event`·`list_events`·`list_logs`, `EventEmitter`·`InMemoryEmitter`·`DbEventEmitter`), Activity 발행은 `cmp_worker.events`(`emit_event`·`emit_event_async`·`log_event`, Workflow용 `emit_migration_event` Activity)다. 읽기는 `GET /migrations/{id}/events`(SSE·JSON)와 `GET /migrations/{id}/logs`. 기존 모델·Activity 반환값은 바꾸지 않는다. Terraform Apply는 `-json`으로 바뀌었고 결과 계약(`status`·`code`·refs)은 같다. 결정은 [migration-events.md E1~E17](migration-events.md), 담당자 사용법은 [이벤트 발행 안내서](../../docs/engineering/events-guide.md).

## .env 업로드와 env_file_ref (ENV-01·ENV-02)

[명세](../env-file-ref/spec.md), [ENV-01](../../tickets/env-file-ref/01-env-upload-store.md), [ENV-02](../../tickets/env-file-ref/02-migration-request-env-ref.md). 업로드 라우트는 운영자 키 `X-Enrollment-Admin-Key`가 필요하고 웹 BFF가 서버 측에서만 호출한다. 응답·오류에는 값이 없고 오류 본문은 `{"detail": {"code": ...}}`뿐이다.

| 라우트 | 요청 | 응답 |
| --- | --- | --- |
| `POST /env-files` | 본문 = `.env` 원본 바이트(`application/octet-stream` 또는 `text/plain`, UTF-8, 64 KiB 이하) | 201 `{env_file_ref: {id: "envf-<32 hex>", version: "1", sha256}, keys: [이름], warnings: [{code: "ENV_KEY_DUPLICATED", key}]}`. `sha256`은 원본 바이트 기준 |
| `POST /env-files/{id}` | 같은 본문 | 201, 같은 `id`의 다음 `version`. 이전 버전은 보관 기간까지 읽을 수 있다 |
| `DELETE /env-files/{id}` | — | 204. 모든 버전 삭제, 반복 호출도 204 |
| `POST /migrations` | 선택 `env_file_ref: ContentRef` 또는 `envf-<32 hex>`(44) | 없으면 지금과 같다. 있으면 저장소에 같은 `version`·`sha256`이 있어야 하고, 아니면 422 `ENV_FILE_NOT_FOUND`·`ENV_FILE_DIGEST_MISMATCH`·`ENV_FILE_EXPIRED`(시작하지 않음). 문자열 id는 시작 순간 최신 버전으로 고정한다. `target`이 있는 웹 시작에서는 필수다(아래 44) |

오류 코드: `ENV_FILE_NOT_FOUND` 404, `ENV_FILE_EXPIRED` 410(삭제·보관 만료), `ENV_FILE_TOO_LARGE` 413, `ENV_FILE_NOT_UTF8` 422, `ENV_FILE_STORE_UNCONFIGURED`·`ENV_FILE_KEY_INVALID` 503.

저장소 `cmp_providers.env_file_store.EncryptedEnvFileStore(root, key)`:
- `put(data, file_id=None) -> (ContentRef, names, duplicates)`, `delete(id)`, `bind_migration(ref, migration_id)`, `purge_expired(ended_at, now=None)`, `bound_migrations()`, `check(ref)`(복호화 없이 존재·sha 확인)
- ENV-03 `EnvFileStore` 프로토콜 메서드: `read_names(ref) -> list[str]`, `read_values(ref, keys) -> dict[str, str]`
- 오류는 `EnvFileError(code)`(`.code`). ENV-03의 오류 형식이 병합되면 하나로 합친다.
- 파싱: 이름 규칙은 `cmp_scanners.envfile.parse_env_names`와 같다(`[export ]NAME=`, 그 밖의 줄 무시). 값은 앞뒤 공백과 한 쌍의 같은 따옴표만 벗긴다(이스케이프·인라인 주석 해석 없음). 중복 키는 마지막 값.
- 암호화: AES-256-GCM(`cryptography`), 버전마다 무작위 96비트 nonce, 연결 데이터 `id|version|sha256`. 파일 `<root>/<id>/v<N>.bin`, 메타 `<root>/<id>/meta.json`(값 없음) 모두 0600, 디렉터리 0700.
- 보관: 사용한 이전의 Workflow가 끝난 뒤 7일, 이전에 쓰이지 않은 업로드는 마지막 버전 업로드 뒤 7일, 또는 `DELETE` 즉시. 실행은 `scripts/purge_env_files.py`(Temporal `close_time` 기준, cron 등록은 배포 측).

API 설정(`cmp_api.config`): `env_file_store_dir`(`ENV_FILE_STORE_DIR`), `env_file_encryption_key`(`ENV_FILE_ENCRYPTION_KEY`, base64url 32바이트, 서버 `.env`에만).

Workflow 전달(patch `env-file-ref-v1`): 시작 입력 dict의 `env_file_ref`를 분석 자식 입력 `AnalysisInput.env_file_ref`(추가 선택 필드), Sandbox `SandboxSettings`·`SandboxInput.env_file_ref`(추가 선택 필드), Planning 인계 설정 `PlanningSettings.env_file_ref`(시작 요청 값이 우선)로 복사한다. `status` 쿼리에 `env_file_ref`가 추가됐다. API의 scaffold 시작(`analysis` 없음)은 `{project_id, env_file_ref}` dict로 시작하며 분석·Sandbox 자식은 여전히 scaffold 문자열 입력이다.

## 웹 시작 요청 → 실제 시작 입력 (44)

[44 티켓](../../tickets/provisioning/44-web-start-to-planning.md#착수-결정-2026-11-06). `cmp_contracts.migration.MigrationRequest`는 웹 `cloud-migration-web` `contract/rest.schema.json` `StartRequest`와 같은 모양이고 모르는 필드는 422다(`extra="forbid"`).

| 필드 | 형식 | 비고 |
| --- | --- | --- |
| `project_id` | 문자열(필수) | |
| `source_provider` | `AWS` | |
| `target_provider` | `AWS`·`GCP`·`ON_PREM`(선택) | `target.provider`와 다르면 422 `TARGET_PROVIDER_MISMATCH` |
| `target` | `{provider, profile, region?, agent_id?, credential_ref?}` | 없으면 scaffold 시작(입력 바이트 동일). 있으면 실제 시작 |
| `source` | `{kind: GITHUB_PUBLIC, repository_id, full_name, commit_sha}` | 실제 시작에 필수(`SOURCE_REQUIRED`) |
| `env_file_ref` | `ContentRef` 또는 `envf-<32 hex>` | 실제 시작에 필수(`ENV_FILE_REF_REQUIRED`) |
| `redteam_cache_enabled` | `bool`(엄격: 문자열·숫자는 422) | 웹 시작 화면의 "보안 점검 결과 재사용" 토글. 서버 `WEB_START_SANDBOX_SETTINGS`가 있을 때만 그 위에 `SandboxSettings.redteam_cache_enabled`로 덮어쓴다(없으면 리허설을 켜지 않으므로 효과 없음). 생략하면 서버 값(기본 켜짐). `target` 없이 보내면 `REDTEAM_CACHE_NOT_APPLICABLE`. 워커에 `REDTEAM_CACHE_BUCKET`이 없으면 어느 값이든 효과 없음 |

실제 시작 입력은 CLI `build_start_input`과 같은 `{project_id, analysis: AnalysisInput{repository: https://github.com/<full_name>.git, requested_commit}, sandbox: null, planning: PlanningSettings(env_file_ref null), env_file_ref}`이고 `MigrationWorkflow`는 바뀌지 않았다. 57(#228)부터 API 설정 `WEB_START_SANDBOX_SETTINGS`(SandboxSettings JSON 객체)가 있으면 `sandbox`가 그 객체(CLI `--sandbox`와 같은 값)이고, 비어 있으면 `null` 그대로다. JSON 객체가 아니면 503 `SANDBOX_SETTINGS_INVALID`. 필드 검증은 worker(`assemble_sandbox_input`)가 한다. data-migration 08부터 API 설정 `WEB_START_DATA_MIGRATION`(DataMigrationInput JSON 객체, CLI `--data-migration`과 같다)이 있으면 `data_migration`이 그 값이고, 없으면 키가 없다(데이터 단계 scaffold). 검증에 실패하면 503 `DATA_MIGRATION_SETTINGS_INVALID`. `PlanningSettings`는 `cmp_api.start_request.planning_settings`가 만든다.

- 대상 Profile 카탈로그(API 설정 `planning_target_profiles_file`, `PLANNING_TARGET_PROFILES_FILE`): `{"profiles": {<profile>: 항목}}`. 항목은 AWS·GCP `{provider, settings: Plan 입력에서 migration_id·plan을 뺀 필드, workloads?}`, ON_PREM `{provider, environment_id?, namespace, storage_class, database_image}`. 예시는 `infra/demo/target-profiles.example.json`. 코드 기본값은 없다. 없으면 503 `TARGET_PROFILES_UNCONFIGURED`.
- 요청이 바꿀 수 있는 값: 클라우드 `region`(→ `settings.region`·`location`), 대상 연결 `credential_ref`(`conn-<hex>` 또는 `cmp-conn-<hex>`, `AWS_TARGET`·`GCP_TARGET`·`CONNECTED`; → `settings.credential_ref = {id: AWS는 cmp-conn-<hex>·GCP는 conn-<hex>, version: "1"}`, AWS `account_id`·GCP `project_id`도 연결 값), ON_PREM `agent_id`(→ `cluster_uid`는 등록 값, `environment_id`는 항목 값 또는 `agent_id`). 나머지는 카탈로그 값 그대로다.
- `TargetSelectionV2`: `ref {id: target-<profile>, version: 대상 설정 sha256 앞 16자}`, `access_ref`(클라우드 `credential_ref`, ON_PREM `{id: agent_id, version: cluster_uid}`), `capabilities_ref {id: profile-<profile>, version: 항목 sha256 앞 16자}`, `profile`.
- 거절 코드(`{"detail": {"code"}}`, 시작하지 않음): 422 `SOURCE_REQUIRED`, `ENV_FILE_REF_REQUIRED`, ENV-02 코드, `TARGET_PROVIDER_MISMATCH`, `TARGET_PROFILE_NOT_FOUND`, `TARGET_PROFILE_PROVIDER_MISMATCH`, `TARGET_FIELD_NOT_APPLICABLE`, `TARGET_REGION_REQUIRED`, `TARGET_AGENT_REQUIRED`, `TARGET_AGENT_NOT_FOUND`(없음·폐기), `TARGET_CONNECTION_NOT_FOUND`, `TARGET_CONNECTION_KIND_MISMATCH`, `TARGET_CONNECTION_NOT_READY`, `TARGET_SETTINGS_INVALID`(`TargetSettings`·05/06 입력 검증 실패); 503 `TARGET_PROFILES_UNCONFIGURED`, `.env` 저장소 코드.
- 추가 공개 함수: `EncryptedEnvFileStore.latest(file_id) -> ContentRef`.

## 샌드박스 결과 → Planning 인계 (55)

[55 티켓](../../tickets/provisioning/55-sandbox-results-into-plan.md#착수-결정-2026-11-06). 받는 쪽만 바꿨고 샌드박스 결과·분석 `assembly` 형식은 그대로다. `MigrationWorkflow`는 바뀌지 않았다(patch 없음).
- 추가 공개 함수: `cmp_worker.sandbox_handoff.sandbox_outcome(result: dict) -> SandboxOutcome{cleanup_status, verified_target_ref}`. 정리 상태는 결과의 `cleanup_status` → `cleanup_failed`(bool) → `CLEANED` 순으로 읽는다. 검증 대상은 결과의 `target_selection_ref`(`VersionRef`)가 있을 때만 읽는다. 있는데 형식이 틀리면 `HandoffError("SANDBOX_RESULT_INVALID")`.
- `assemble_registration(..., cleanup_status="CLEANED")` 선택 인자 추가(기본값이면 이전 바이트 그대로). `sbx_plan_assemble`은 어댑터 값을 `cleanup_status`와 `expected_target_ref`(입력 `target_selection_ref`가 없을 때)로 넘긴다.
- 결과: 정리 상태가 `CLEANED`가 아니면 Plan Revision에 `CLEANUP_POLICY_REQUIRED`(승인 불가), 기록된 대상이 Planning 대상과 다르면 인계 거절 `TARGET_SELECTION_CHANGED`, 형식 오류는 `SANDBOX_RESULT_INVALID`(인계 `REFUSED` → Migration `failed`).
- EnvPlan은 #197 경로(`assembly.env_plan_ref` → `assemble_registration(env_plan=...)`) 그대로이고, `PlanningSettings`에는 싣지 않는다(44 S12).

## 구축 env 렌더 (ENV-04)

[ENV-04](../../tickets/env-file-ref/04-provisioning-render-env.md#착수-결정-2026-11-06). 추가 공개 함수: `cmp_providers.env_file.preview_env`·`database_outputs`·`deployment_secrets`, 저장소 연결 `cmp_api.env_store.configured_env_file_store`·`optional_env_file_store`. 계약 추가(모두 선택, 없으면 이전 바이트 그대로): `PlanningInputRegistration.env_plan: EnvPlan`, `RolloutApplication.env_secrets: [RolloutEnvSecret{name, keys}]`. SPEC 산출물 JSON에 `env_plan`이 생긴다(있을 때만). `onprem_preview(..., platform_secrets=...)` 선택 인자. HTTP: `GET /agents/{agent_id}/jobs/{job_id}/env-secrets` → `{secrets: [{name, data}]}`(Agent 인증, 자기 RUNNING 작업, `no-store`; 404 `ENV_SECRETS_NOT_FOUND`, 409 승인·저장소·결정 코드, 503 저장소 미설정). `GkeKubectlSession.apply_secret`는 공통 3인자 형태도 받는다.

**AWS 대상 권한·사용자 클러스터 접근 (47):** [티켓 47](../../tickets/provisioning/47-aws-target-access.md).
- 대상 연결 역할 템플릿 v2(`infra/connections/target-role.yaml`, 버킷 `connections/v2/`)가 허용·거부할 호출 목록은 `cmp_providers.aws_target_access`(`TERRAFORM_CALLS`, `WORKLOAD_CALLS`, `refusals`, `allowed_cases`, `render_target_policies`)다. Terraform 리소스 타입을 바꾸면 이 목록과 템플릿을 같이 고친다(`tests/test_aws_target_access.py`가 강제).
- AWS Plan 입력 추가(선택, 기본 `[]`): `cluster_admin_principal_arns`(IAM 역할·사용자 ARN 최대 10개, 경로·다른 계정 허용, root·STS 세션·서비스 연결 역할 거절 `INVALID_PRINCIPAL_ARN`, 중복 `DUPLICATE_PRINCIPAL_ARN`). 템플릿 `aws-eks-v4`. 비면 Preview 경고 `NO_USER_CLUSTER_ACCESS`. Apply 결과 `resource_refs.eks_cluster_admin_principal_arns`(쉼표 구분)가 인계 표시용이다.
- `plan_aws_infrastructure(..., platform_egress_cidrs=())`: 주면 각 CIDR이 `api_access_cidrs` 중 하나에 포함돼야 한다(`PLATFORM_EGRESS_NOT_ALLOWED`, 설정 형식 오류 `INVALID_PLATFORM_EGRESS`). 비면 Preview 경고 `PLATFORM_EGRESS_UNCHECKED`. 공용 검사는 `cmp_providers.platform_egress.check_platform_egress`(GKE Plan도 쓸 수 있다). worker 설정 `PROVISIONING_PLATFORM_EGRESS_CIDRS`.
- ECR pull 토큰: api `platform_ecr_aws_profile`과 worker GKE 갱신(`PLATFORM_ECR_AWS_PROFILE`, 없으면 `PROVISIONING_AWS_PROFILE`)이 같은 pull 전용 프로필을 쓴다. Build 모듈은 `ImageBuilder(context, aws_profile=)`로 자기 `aws` 호출에 프로필을 준다.

## 이미지 pull 자격 (29)

[티켓 29](../../tickets/provisioning/29-image-pull-strategy.md#착수-결정-2026-11-06). 공개 모듈: `cmp_providers.ecr_pull`(`PlatformRegistry.parse/require`, `EcrTokenMinter.mint → PullCredential`, `boto3_minter`, `dockerconfigjson`, `PULL_SECRET="cmp-ecr-pull"`), `cmp_providers.agent_image_pull`(`PullSecretRefresher`, `image_pull_failed`). 계약 추가(선택, 없으면 이전 바이트 그대로): `ProductionRolloutJob.image_pull: RolloutImagePull{secret, registry}`. 이전 Agent는 이 필드가 있는 작업을 `UNSUPPORTED_JOB`으로 거절한다. HTTP: `GET /agents/{agent_id}/jobs/{job_id}/image-pull` → `{registry, username, password, expires_at}`(Agent 인증, 자기 RUNNING 작업 또는 아직 최신 승인 Revision인 SUCCEEDED 작업, `no-store`; 404 `IMAGE_PULL_NOT_FOUND`, 409 `IMAGE_REGISTRY_NOT_ALLOWED`, 503 `PLATFORM_REGISTRY_INVALID`·`ECR_TOKEN_UNAVAILABLE`·`ECR_ACCOUNT_MISMATCH`). API 설정: `platform_ecr_registry`(`PLATFORM_ECR_REGISTRY`), `platform_ecr_repositories`(`PLATFORM_ECR_REPOSITORIES`, 기본 `cmp-platform-builds`). `build_rollout_job(..., registry=None)` 선택 인자. Activity `refresh_gcp_pull_secrets` → `{refreshed, skipped, failed, codes}`, Workflow `GcpPullSecretRefreshWorkflow`(입력 없음). Agent 실패 코드: `SECRET_NAME_CONFLICT`, `IMAGE_PULL_NOT_CONFIGURED`, `IMAGE_PULL_CREDENTIALS_UNAVAILABLE`(UNKNOWN), `IMAGE_PULL_CREDENTIALS_INVALID`, `IMAGE_PULL_FAILED`.

## GCP 주체 확인과 연결 확인 (48)

[티켓 48](../../tickets/provisioning/48-gcp-target-principal.md#착수-결정-2026-11-06). 공개 모듈: `cmp_providers.gcp_adc`(`read_adc_source → AdcSource(kind, declared)`, `token_command`, `adc_principal(env, cwd=) → str | None`). `gcp_preview.verify_adc_principal`·`_verify_adc`와 `gcp_apply.default_adc_email`이 이 함수를 쓴다. 코드는 그대로 `ADC_PRINCIPAL_MISMATCH`·`ADC_UNAVAILABLE`(토큰 명령 실패 포함, 이전 Plan은 `PLAN_COMMAND_FAILED`)·`ADC_CHECK_FAILED`·`ADC_SOURCE_UNSUPPORTED`. Temporal: 상수 `cmp_contracts.queues.GCP_CONNECTION_CHECK_WORKFLOW_NAME = "GcpConnectionCheckWorkflow"`(`migration` 큐, 입력 예상 주체 이메일 문자열)와 `GCP_CONNECTION_CHECK_ACTIVITY = "verify_gcp_connection"`(`provisioning` 큐) → `{code: "OK" | ADC_*, principal: str | None}`. API: `cmp_api.gcp_connection_check.TemporalGcpChecker`(연결 확인 실패 코드 추가 `GCP_CHECK_UNAVAILABLE`·`GCP_CHECK_FAILED`). HTTP 경로·응답 모양은 바뀌지 않았다.

## 멀티 아키텍처 빌드 (58)

[티켓 58](../../tickets/provisioning/58-multiarch-build.md#착수-결정-2026-11-07). worker 설정 `BUILD_PLATFORM`은 한 값 또는 쉼표 목록(`linux/amd64,linux/arm64`)이다. 허용 값 밖·중복·빈 항목은 `BUILD_PLATFORM_INVALID`. **첫 항목이 Sandbox 아키텍처**다.
- 공개 함수: `cmp_worker.image_build.parse_platforms(value) -> list[str]`, `PLATFORMS`.
- Build 모듈·연결 모델(`BuildContext`·`BuildOutcome`·`BuildTarget`·`BuildResult`·`BuildSettings`): `platforms: list` 필드, `platform`은 첫 항목 읽기 속성. 생성 때 `platform=` 단일 값도 받는다. `BuildStore.find(resource_id, source_sha256, platforms)`.
- 여러 플랫폼이면 `image_digest`·`image_uri`는 매니페스트 목록의 Digest다(플랫폼별 이미지 태그 `<태그>-amd64`·`<태그>-arm64`, 목록 태그 `<태그>`). 새 실패 코드 `BUILD_INDEX_INVALID`·`BUILD_INDEX_PLATFORM_MISMATCH`(분류 `BUILD`).
- 계약 추가(없으면 `[platform]`으로 읽음): `BuildAttempt.platforms`(`platform`은 Sandbox 실행 플랫폼, 목록 안에 있어야 함), `ValidatedImageV2.platforms`(빌드 증거 목록). `BuildEvidenceV2.platforms`는 원래 목록이고 이제 목록 전체가 들어간다.
- Plan(ON_PREM·클라우드 WORKLOADS)의 앱·DB `image.platforms`는 `ValidatedImageV2.platforms`다. Chart의 노드 조건(`kubernetes.io/arch In`)이 이 목록 전체를 허용한다.
- HTTP·OpenAPI·Workflow 변경 없음.

