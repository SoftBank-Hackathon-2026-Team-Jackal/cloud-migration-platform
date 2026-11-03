# 앱 Chart·환경변수 바인딩 — Ticket 03

Issue #13은 검증된 앱 이미지와 명시적인 환경변수 메타데이터를 받아 앱 Values 템플릿·바인딩 규칙·Deployment/Service Manifest를 만든다. Sandbox/Production, AWS/GCP/ON_PREM은 같은 Chart를 사용한다. 변수 이름을 추측하거나 `.env` 파일을 이 경계에서 파싱하지 않는다.

## 입력과 변환

`cmp_contracts.application.ApplicationValues`는 내부 v1 계약이다. `config_ref`로 별도 업로드 설정의 ID·버전·Digest를 추적하고 `required_env`와 `env`의 키 집합이 정확히 일치해야 한다. 업스트림의 실제 출력 형식과 변환·메타데이터 분류는 호출자 책임이다. 일반 값도 원문 대신 이미 준비했거나 준비할 ConfigMap의 이름/키를 전달한다.

| 입력 | 렌더 결과 |
| --- | --- |
| image URI(Digest 고정), application CPU/메모리/replicas | Deployment 이미지·requests/limits·복제 수 |
| application port/health_path | ClusterIP Service, containerPort, readiness HTTP 경로 |
| CONFIG_MAP 이름/키 | env.valueFrom.configMapKeyRef |
| USER_SECRET 이름/키 | env.valueFrom.secretKeyRef |
| DATABASE host/port/name/username | 대상 DB 연결값을 env.value로 전달 |
| DATABASE secret_ref | 생성된 DB Secret의 이름/키를 env.valueFrom.secretKeyRef로 전달 |

DB port/name/username/secret_key는 계획 시 고정한다. `database.host`와 `database.secret_name`은 계획 시 null이며, 사용하는 변수에 한해서 기존 `DynamicBinding` 규칙으로 실행 결과를 주입한다. DB 비밀번호 원문은 받지 않는다. 사용자 Secret과 생성 DB Secret은 별도 출처로 유지한다. DB 변수를 안 쓰는 앱은 동적 바인딩 없이 렌더할 수 있다.

## 공개 함수와 승인 경계

- `prepare_application(payload)` → `ApplicationPlan(values, bindings)`: 타입·지원 범주·필수 키·중복·참조 형식을 검사한다. 값이 채워진 동적 슬롯은 계획 입력으로 거절한다. 반환 모델은 참조와 키 메타데이터이므로 Preview 작성에 사용할 수 있다. 전체 Plan Preview/승인을 발급하지는 않는다.
- `resolve_application(template, resolved)` → 최종 Values dict: 기존 공통 `bind_values`를 사용한다. 필요한 source가 빠지거나 남으면 거절한다.
- `verify_application(template, actual, resolved)`: 기존 `verify_bound_values`로 최종 Values 전체를 비교한다. 허용된 위치 외 값/참조/이미지/자원/매핑 변경은 거절한다.
- `render_application(payload, chart_dir, helm_bin)` → 정확한 Manifest bytes: 최종 입력과 미해결 DB 바인딩을 확인한 후 실제 Helm을 호출한다. 임시 파일은 호출 후 정리하며 Helm 오류 출력은 공개 오류에 복사하지 않는다. 외부 I/O 함수이므로 Workflow에서 실행하지 않고 후속 Activity에서 호출한다.

호출자는 **신뢰 가능한 최신 승인 기록과 실제 Chart/Values/규칙 바이트의 Digest**를 확인하고, 승인된 템플릿 및 실제 대상 DB 결과로 `verify_application`을 호출해야 한다. 클라이언트가 제출한 템플릿·주소만으로 승인이나 실제 배포 성공을 증명하지 않는다. 템플릿은 `prepare_application(...).values.model_dump(mode="json")`의 명시적 기본값/null을 포함한 구조로 저장한다. 저장 ArtifactRef·RenderedChart 발급, Revision 저장, 실제 승인 게이트 연결은 후속 작업이다.

## 재현

Helm 3.17.3을 설치하고 아래 예제를 저장소 루트에서 실행한다. fixture의 이미지·참조는 테스트 데이터다.

```python
import json
from pathlib import Path
from cmp_providers.application import prepare_application, resolve_application, verify_application
from cmp_providers.app_renderer import render_application

payload = json.loads(Path("tests/fixtures/contracts/app-values.json").read_text())
plan = prepare_application(payload)
template = plan.values.model_dump(mode="json")
resolved = {"DATABASE_HOST": "db.sandbox-001.svc", "DATABASE_SECRET_NAME": "db-auth"}
values = resolve_application(template, resolved)
verify_application(template, values, resolved)
manifest = render_application(values, chart_dir=Path("infra/helm/application"))
```

`HELM_BIN=/path/to/helm uv run pytest tests/test_app_render.py`로 공개 함수 및 실제 Helm lint/template를 검증한다. Chart의 JSON schema는 ApplicationValues에서 생성했으며, Python validator의 키 집합·중복·미해결 슬롯 검사는 Helm 템플릿에서도 검사한다. 해결되지 않은 `image.registry_auth_ref`는 DB Chart와 동일하게 거절한다.

## 보안과 미검증 범위

공개 함수는 원문 입력·임의 필드명·Pydantic context·Helm stderr를 오류/로그로 노출하지 않는다. raw `.env`, 임의 env.value 및 비밀번호 필드는 허용하지 않는다. 신뢰할 수 없는 원문을 직접 Helm CLI에 전달하면 Helm 자체 오류가 입력 일부를 출력할 수 있으므로 서비스는 검증/오류 정제 경계를 우회하면 안 된다. 이 계약은 정상 메타데이터 필드에 잘못 분류된 비밀값을 자동 판별하는 기능이 아니며, 업스트림의 일반 설정/Secret 분류와 참조 접근 권한 검증이 필요하다.

ConfigMap/Secret 생성·조회·회전·권한 확인, Registry 인증 연결, Ingress/LB, 클러스터 설치, 실제 이미지/DB/앱 헬스 검증은 하지 않는다. 포트/DB 이름/사용자를 포함한 연결 메타데이터만 Manifest에 들어가고 비밀번호/API Key는 참조로 남는다. 대상 Namespace와 ConfigMap/Secret은 실행 전에 준비해야 한다. 본 테스트 통과는 실배포나 Data/Traffic Migration 완료를 의미하지 않는다.
