# 최소 공유 계약 — CP-01

## 구현 범위

[Issue #9](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/9)는 순차 개발에서 DB Chart, 클라우드 Plan 및 승인 게이트가 공유하는 내부 v1 모델과 순수 함수를 제공한다. HTTP/DB 승인 기록, Temporal 연결, S3 저장·권한 조회, Terraform/Helm 실행은 포함하지 않는다. 02부터 이 계약을 실제 산출물 생성에 연결한다. 문서 변경은 해당 구현 이슈/PR에 포함한다.

## 공개 모델·함수

| 경계 | 구현 위치 | 역할 |
| --- | --- | --- |
| `ArtifactRef`, `RenderedChart` | `cmp_contracts.artifact` | 불변 ID·버전·종류·바이트 크기·SHA-256 및 Chart/Values/Manifest 참조 |
| `PostgresChartValues` | `cmp_contracts.chart` | Sandbox/Production 공통 DB 입력. 환경·Provider·Release·Namespace·이미지·DB·Pod 자원·StorageClass/PVC |
| `DynamicBinding` | `cmp_contracts.chart` | 허용된 DB host/Secret 이름/URL Secret 이름 위치와 값 출처 |
| `connection_url`, `connection_url_secret_name` | `cmp_providers.database_url` | DB 연결 URL 형식과 URL Secret 이름 규칙(19). Sandbox·운영 실행 주체가 함께 사용 |
| `PlanManifest`, `Preview`, `ApprovalTarget` | `cmp_contracts.plan` | Plan/Revision·템플릿 버전·대상·실행 범위·산출물 연결, 표시 요약, 승인 대상 식별 |
| `sha256_bytes`, `canonical_json_bytes`, `sha256_json`, `verify_artifact_bytes` | `cmp_providers.artifacts` | 순수 바이트/정규화 JSON Digest 및 조회한 실제 바이트 검증 |
| `approval_issues`, `approval_target`, `verify_approval_target` | `cmp_providers.plans` | 계획 완전성·Preview 일치·기록된 승인 대상 및 요청 실행 범위 비교 |
| `bind_values`, `verify_bound_values` | `cmp_providers.bindings` | 승인된 동적 자리만 채우고 나머지 Values 변경 거절 |

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

`cmp_api.onprem_rollout`이 07의 승인 기록·`latest_revision`·`manifest_digest`와 `verify_artifact_bytes`를 재사용해 Agent 작업을 만들고, 04A의 `agent_jobs`·인증·결과 보고와 04B의 Values 모델을 그대로 쓴다. 새 공통 모델은 Agent 작업 본문 `cmp_contracts.onprem_rollout.ProductionRolloutJob`뿐이다. 실행 dispatch는 08 소유 계약을 따른다. 상세는 [공통 배포 계약](onprem-shared-deployment.md#승인된-운영-배포-12-36)을 따른다.

## 온프레미스 Preview 생성 (12A)

15(#69)는 새 공유 계약을 만들지 않는다. 04A 등록 계약의 `cluster_uid`를 발급 시 선택으로 바꾸고 Agent 마지막 접속·폐기 상태를 추가했다. 상세는 [공통 배포 계약 §상시 실행·설치·폐기](onprem-shared-deployment.md#상시-실행설치폐기-15-69)를 따른다.

`cmp_providers.onprem_preview.onprem_preview(manifest, contents)`는 ON_PREM WORKLOADS/FULL 계획이 고정한 `database_values`·`application_values` 바이트를 `verify_artifact_bytes`로 확인하고 공통 `Preview`를 만든다. 보여 주는 내용은 기존 K3s·Agent·Namespace, DB StatefulSet·PVC, 앱 Deployment·Service, 외부 공개 없음, 적용 기본값(Namespace 생성, DB 비밀번호 Secret 생성)이다. 클러스터를 조회하지 않으므로 경고 `CLUSTER_STATE_NOT_INSPECTED`를 붙인다. 사용자 Secret은 이름·키만 `USER_SECRET_REQUIRED` 경고로 표시하고 환경변수·Secret 값은 넣지 않는다. Values의 대상·이미지가 manifest와 다르면 `errors`에 `VALUES_TARGET_MISMATCH`/`IMAGE_MISMATCH`를 넣어 `approval_issues`가 승인을 거절하게 한다. Digest 불일치·비 ON_PREM은 `ProvisioningInputError`로 Preview를 만들지 않는다. Planning 저장 경계와의 연결은 18이 구현했다(아래 §18). 상세는 [12A 티켓](../../tickets/provisioning/12a-onprem-preview.md)을 따른다.

## Provisioning 결과 인계 (13)

`cmp_contracts.provisioning_result.ProvisioningResult` v1이 Provisioning → Data/Traffic Migration 인계 계약이다. `cmp_providers.provisioning_result`의 `build_provisioning_result`(실행 상태·코드·단계·refs → 결과), `handoff`(소비자별 뷰·`proceed`/`blocked_by`), `public_summary`(사용자 요약)는 순수 함수다. 저장·조회는 `cmp_api.provisioning_results`(`add_result`/`load_result`, 테이블 `provisioning_results`), Worker 진입점은 Activity `get_migration_handoff`, 사용자 조회는 `GET /provisioning/plans/{plan_id}/revisions/{revision}/result`다. 필드·판정 규칙은 [티켓 13 결정 R1~R10](../../tickets/provisioning/13-result-handoff.md#착수-결정-2026-11-05)을 따른다. Provider 실행 refs의 표준 키(R4)는 이 결과의 입력이며 기존 키는 유지한다.

## 터널 설정 교환 (16)

`cmp_api.tunnels`와 `cmp_api.routes.tunnels`가 Agent 공개키 등록(응답에 중계 서버 목록 포함)·터널 IP 배정·Agent 터널 설정 조회·터널 상태 보고·중계 서버 등록·중계 서버용 Agent 목록(`GET /relays/{relay_id}/agents`, 중계 서버 자격 증명, 폐기 Agent는 `revoked: true`로 남음, 내용 digest인 `list_version`)을 제공한다. 결과 계약에는 `DatabaseHandoff.tunnel_endpoint`와 `ApplicationHandoff.external_host`·`external_port`가 추가됐다. `external_endpoint`는 "중계 서버가 전달할 목적지"이며 호환을 위해 유지하고, 같은 목적지를 host·port로도 준다(AWS/GCP는 LB 주소·80). `get_migration_handoff`는 ON_PREM 결과에 최신 터널 상태를 읽는 시점에 합친다. Data 인계는 터널 경유 DB 접속 확인 뒤, Traffic 인계는 터널 경유 앱 헬스 확인 뒤(`http://터널IP:앱포트`)에만 진행한다. 중계 서버 공인 주소는 어느 인계 필드에도 없다. 상세는 [티켓 16 T1~T13](../../tickets/provisioning/16-tunnel-config-exchange.md)을 따른다.

## Plan 생성·게시와 Preview 조회 (18)

새 공유 모델은 `cmp_contracts.plan_publish`의 `PlanningInputRegistration`(Planning 입력 v2 + Provider별 대상 설정 + 선택 요구사항 바이트)과 `plan_id_for`(Migration당 Plan ID)뿐이다. 기존 `PlanManifest`·`Preview`·`approval_target`·`onprem_preview`·05/06 Plan 함수는 바꾸지 않고 호출한다. `publish_plan_revision`은 선택 인자 `input_digest`를 받아 같은 트랜잭션에 `provisioning_plan_publications`를 기록한다(기존 호출 동작은 그대로). `ProvisioningApprovalWorkflow` 요청은 선택 필드 `approval_timeout_seconds`를 받아 승인 없이 기간이 지나면 `EXPIRED/APPROVAL_EXPIRED`로 끝난다. 상세는 [18 티켓](../../tickets/provisioning/18-onprem-plan-publish.md#착수-결정-2026-11-05)을 따른다.

## AWS/GCP 워크로드 Plan 게시 (22)

22(#108)는 공유 모델에 하위 호환 필드만 더한다. `AwsTargetSettings`·`GcpTargetSettings`의 선택 필드 `workloads: CloudWorkloadSettings{namespace, storage_class, database_image}`와 `workload_plan_id_for`(Migration당 워크로드 Plan ID)다. 18 입력 Digest는 `workloads`를 빼고 계산해 기존 등록의 Digest가 바뀌지 않는다. 인프라 Revision 실행 결과(업무 DB refs)로 별도 `WORKLOADS` Plan을 게시하며, manifest `target`은 인프라 manifest와 같아 09·11이 그 인프라 실행의 클러스터를 찾는다. 앱 Values는 09·11의 v1 `ApplicationValues` 템플릿이고 산출물은 `PROVISIONING_ARTIFACT_ROOT/<plan_id>-r<revision>/<sha256>`에 둔다. Preview는 `cmp_providers.cloud_workload_preview`(순수 함수). `PlanManifest`·`Preview`·`ProvisioningResult`·승인·실행 계약은 바꾸지 않는다. 상세는 [22 티켓](../../tickets/provisioning/22-cloud-workload-plan.md#착수-결정-2026-11-05).

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
 "tunnel": {"tunnel_ip": "10.200.0.2", "last_handshake_at": 0, "app_health_ok": true, "db_connect_ok": true}}
```

- `online`: 폐기되지 않았고 `last_seen_at`이 60초 이내다.
- `tunnel`: 16의 마지막 `agent_tunnel_status` 보고다. 보고가 없으면 `null`이다.
- `version`: Agent 버전 보고가 후속이라 `null`이다.

**`credential_ref` 해석:** 역할이 등록된 AWS 연결의 `credential_ref`는 `cmp-conn-<연결 hex>`다. 이 이름은 생성된 AWS config 파일(`connection_aws_config_file`)의 프로필이다. 프로필은 `role_arn`, `external_id`, `source_profile = connection_platform_aws_profile`, `role_session_name`, `duration_seconds = 3600`, `region`이다. 분석 `cmp_scanners.aws_infra.get_session(credential_ref)`과 Provisioning `aws_profile`에 그대로 넘긴다. 프로세스는 `AWS_CONFIG_FILE`이 이 파일을 가리켜야 한다. 대기 중인 연결과 GCP 연결은 `credential_ref: null`이다. GCP 데모는 기존 `provisioning_gcloud_configuration`·ADC 설정을 쓴다.

API 설정(`cmp_api.config`):
- `connection_platform_aws_profile`: verify가 쓰는 플랫폼 주체 프로필이자 `source_profile`
- `connection_platform_principal_arn`: 템플릿 신뢰 대상
- `connection_template_base_url`: S3 템플릿 접두어
- `connection_aws_config_file`, `connection_aws_base_config_file`
- `connection_gcp_project_id`
