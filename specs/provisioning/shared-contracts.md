# 최소 공유 계약 — CP-01

## 구현 범위

[Issue #9](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/9)는 순차 개발에서 DB Chart, 클라우드 Plan 및 승인 게이트가 공유하는 내부 v1 모델과 순수 함수를 제공한다. HTTP/DB 승인 기록, Temporal 연결, S3 저장·권한 조회, Terraform/Helm 실행은 포함하지 않는다. 02부터 이 계약을 실제 산출물 생성에 연결한다. 문서 변경은 해당 구현 이슈/PR에 포함한다.

## 공개 모델·함수

| 경계 | 구현 위치 | 역할 |
| --- | --- | --- |
| `ArtifactRef`, `RenderedChart` | `cmp_contracts.artifact` | 불변 ID·버전·종류·바이트 크기·SHA-256 및 Chart/Values/Manifest 참조 |
| `PostgresChartValues` | `cmp_contracts.chart` | Sandbox/Production 공통 DB 입력. 환경·Provider·Release·Namespace·이미지·DB·Pod 자원·StorageClass/PVC |
| `DynamicBinding` | `cmp_contracts.chart` | 허용된 DB host/Secret 이름 위치와 값 출처 |
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

실행 scope는 정확히 일치해야 한다. INFRASTRUCTURE 승인을 WORKLOADS/FULL 실행에 재사용할 수 없으며 FULL 승인의 일부만 실행하려는 경우도 현재 함수는 별도 범위 승인을 요구한다. 변경된 target 계정/State, Spec·Plan·Chart/Values Digest, 이미지·템플릿·Revision 또는 Preview는 기존 승인 대상과 일치하지 않는다. 저장소에서 같은 Revision을 덮어쓰지 않고 새 Revision으로 발급하는 책임은 후속 Planning 저장 경계에 있다.

## DB Chart 입력과 동적 바인딩

`PostgresChartValues`는 SANDBOX/PRODUCTION 및 AWS/GCP/ON_PREM에서 같은 구조다. 클라우드 Credential을 받지 않는다. PostgreSQL 16의 이름·사용자, Secret 이름/키, Pod CPU millicores·메모리 MiB, PVC GiB·StorageClass를 명시한다. Node 디스크와 PVC를 혼동하지 않는다. 이미지 URI는 Digest에 고정한다. Registry 인증이 필요한 경우에만 참조를 제공한다.

이름은 초기 구현에서 보수적인 Kubernetes label 형태, Secret key는 영숫자·점·밑줄·하이픈으로 제한한다. 이미지가 실제 PostgreSQL 16인지, Secret/PVC가 존재하는지는 렌더/실환경 검증에서 확인한다. DB Chart의 Helm 템플릿은 02에서 `infra/helm/postgres`에 구현했다. 사용법·렌더 제한은 [Helm 안내](../../infra/helm/README.md)를 따른다. 현재 Chart는 해결되지 않은 `registry_auth_ref`를 거절하며, 실제 Secret/StorageClass 존재·이미지 실행 검증은 후속 배포 경계다.

동적 바인딩은 `database.host ← DATABASE_HOST`, `database.secret_name ← DATABASE_SECRET_NAME`만 지원한다. 이는 런타임 DB 연결을 쓰는 Values 템플릿의 자리이며 DB Chart에 host 필드를 추가한다는 뜻은 아니다. 대상 자리가 미리 존재하고 null이어야 한다. 비밀번호 원문 대신 생성된 Kubernetes Secret **이름**을 전달한다. 제공한 source key가 빠지거나 남으면, 경로가 중복되거나 source/target이 어긋나면 실패한다. 원본 템플릿은 변경하지 않는다.

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
