# Provisioning Planning 입력 계약 v2

## 상태와 공개 경계

Issue [#1](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/1)의 구현 계약이다. [`cmp_contracts.provisioning_v2.PlanningRequestV2`](../../packages/contracts/src/cmp_contracts/provisioning_v2.py)와 [`cmp_worker.provisioning_v2.prepare_plan_v2`](../../apps/worker/src/cmp_worker/provisioning_v2.py)가 공개 로컬 경계다. 기존 `cmp_contracts.provisioning`/`prepare_plan`의 v1은 당시 테스트 호환용이며 Build→Sandbox 동일 이미지 정책의 증거로 사용할 수 없다.

호출자는 Analysis 결과의 `requirements_ref`를 권한 확인 후 조회하여 **저장된 정확한 바이트**를 `requirements_bytes`로 전달한다. 함수는 그 바이트의 SHA-256을 확인하고 JSON 요구사항을 파싱한다. 다른 참조(소스, Sandbox 보고서, 테스트 묶음, `.env`/mapping, 대상 capabilities)의 실제 조회·소유권·바이트 Digest 확인은 이 순수 함수에 없다. 이를 담당하는 Activity가 생기기 전에는 이 결과만으로 최종 Plan/Preview를 게시하거나 승인하지 않는다.

```python
from cmp_worker.provisioning_v2 import prepare_plan_v2

# envelope: 상위 Workflow가 전달한 작은 메타데이터. requirements_bytes는
# 호출 Activity가 권한·버전을 확인해 읽은 산출물의 정확한 바이트다.
result = prepare_plan_v2(envelope, requirements_bytes=requirements_bytes)
# result.spec은 Plan 입력이다. 아직 Plan ID·Revision·Preview나 배포 성공이 아니다.
```

## Analysis 요구사항과 리소스 연결

`requirements_ref={id,version,sha256}`가 가리키는 JSON은 `contract_version="2"`, `resources[]`, `network_intent[]`로 구성한다. 리소스마다 고유 `id`와 `kind`가 있다. P0는 `APPLICATION`, `POSTGRESQL`만 수용하고 다른 종류는 오류다. 최소 한 앱과 한 DB가 필요하다. 앱은 CPU millicores·메모리 MiB·복제 수·포트·헬스 경로를, PostgreSQL은 버전 16·DB 이름/사용자·CPU·메모리·PVC GiB와 `extensions`를 기록한다. 현재 확장 설치·검증은 없으므로 `extensions`가 비어 있지 않으면 계획 입력을 거절한다. DB 이미지는 이 요구를 만족하는 Digest를 후속 대상 구성에서 선택·검증하며 Profile이 요구 버전을 덮어쓰지 않는다.

앱의 `env_bindings`는 `{resource_id,output}` 참조다. 현재 지원 출력은 PostgreSQL의 `host`, `port`, `name`, `username`, `password_secret_ref`뿐이다. 미존재 ID나 다른 종류의 출력 요청은 오류다. `network_intent`는 출발 리소스와 내부 리소스 또는 외부 호스트 목적지, 포트·프로토콜을 기록한다. Sandbox와 운영 Worker는 같은 의도를 각 환경 정책으로 변환한다. Sandbox의 원본 클라우드 차단 등 환경 전용 제약은 별도이며 이 입력 검증 함수는 NetworkPolicy를 만들지 않는다.

`analysis.source_refs`는 **APPLICATION ID별** SHA-256 이름의 S3 `.tar.gz` URI, 선택적 S3 Version ID, SHA-256을 담는다. 실제 파일 내용은 Build가 다운로드 후 검증한다. `.env` 외의 수정 설정 산출물이 실제로 있을 때만 `analysis.config_artifact_ref`를 사용하며, 이때 Sandbox가 같은 참조를 검증했다고 명시해야 한다.

## Build·Sandbox·대상 대조

하나의 APPLICATION ID에 하나의 성공한 Build와 하나의 Sandbox 검증 기록이 정확히 대응해야 한다. Planning은 다음을 모두 검사한다.

| 대조 항목 | 조건 |
| --- | --- |
| 요구사항 | Analysis의 `requirements_ref`와 Sandbox의 참조가 ID·버전·SHA까지 같고, 조회한 바이트의 SHA도 일치 |
| 소스 | Build의 S3 URI·Version ID·SHA가 해당 앱의 Analysis 소스와 같고, Sandbox의 소스 SHA도 같음 |
| 이미지 | Build의 `image_uri`에 박힌 Digest와 반환 `image_digest`가 같고, Sandbox가 실행한 Digest도 같음 |
| Build/플랫폼 | Sandbox의 `build_ref`가 해당 Build 결과 참조와 같고, 실행 플랫폼이 Build 지원 플랫폼에 포함됨 |
| 설정 | 별도 `config_artifact_ref`가 있으면 Analysis·Sandbox 참조가 정확히 같음 |
| 상태 | Build `SUCCEEDED`, Sandbox `PASSED`; 중복·누락·추가 앱 기록 거절 |

Sandbox `cleanup_status`가 `CLEANED`가 아니면 결과에 `CLEANUP_POLICY_REQUIRED` 미해결 결정을 남긴다. 검증 성공이 정리 성공을 의미하지 않는다. Sandbox의 `report_ref`와 `test_suite_ref`는 보존한다. **배포 직후에는 준비 상태·헬스·기본 연결만 확인**하고, 데이터 이전 후 트래픽 전환 전에 테스트 묶음을 실행하는 것이 최신 운영 순서다.

공통 빌더의 `BuildContext`는 `migration_id`, `resource_id`, `attempt_id`, 플랫폼, ECR 저장소와 로그 버킷을 받는다. 호출 시 소스 URI와 **있다면** S3 Version ID를 전달한다. 같은 시도에서 서로 다른 앱은 다른 ECR 태그와 로그 경로를 사용하고, 결과에는 앱 ID·소스 버전·Digest·로그 참조를 포함한다. URI의 해시 이름을 기준으로 아카이브 내용을 검증한다. 빌드 시도/이미지 결과가 실제로 어떤 Migration 기록에 속하는지는 상위 Activity가 소유권 조회로 확인해야 한다.

## 대상·환경과 Plan 시점

`target_selection`에는 선택 기록 참조, Provider(`AWS`/`GCP`/`ON_PREM`), Cloud 위치 또는 ON_PREM의 null, 접근 참조, capabilities 참조가 필수다. 이름 붙은 `profile`은 선택적 기본값일 뿐 요구사항을 덮어쓰지 않는다. capabilities의 아키텍처·StorageClass·이미지 접근 여부와 실제 Provider 권한은 후속 대상 구성 Activity가 조회·검증한다. `environment`의 `.env` 파일·mapping 참조를 Worker가 처리해 **최종 변수별 바인딩**을 만들고 Secret을 준비한다. 이 순수 경계에는 원문 `.env`·Secret·Credential을 넣지 않는다.

`prepare_plan_v2`는 검증된 요구·출처·대상·환경 참조와 앱별 고정 이미지를 `ProvisioningSpecV2`로 반환한다. **입력에 Plan ID·Revision을 요구하지 않는다.** 후속 Planning 저장 경계가 Plan ID/Revision과 불변 산출물·Preview를 발급하고, 승인 후 실행은 그 정확한 산출물을 재검증한다. v2 Spec은 기존 단일 앱/DB `cmp_providers.provisioning.adapt`나 `render_shared_deployment`에 바로 넣지 않는다. 다중 리소스 렌더·대상별 변환과 Temporal 결과 전달은 다음 구현 작업이다.
