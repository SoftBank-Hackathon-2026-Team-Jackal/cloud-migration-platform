# 에이전트 작업 안내: 분석·매핑·Sandbox 검증

이 문서는 코딩 에이전트가 `analysis-verification` 티켓을 구현하기 전에 읽는 안내서다. 요구사항과 설계는 여기서 반복하지 않는다. 아래 문서들이 기준이고, 이 문서는 **읽는 순서, 작업 범위, 이미 정해진 결정, 재사용할 v2 모델, 진행 절차**만 정리한다.

## 1. 읽는 순서

1. `AGENTS.md`: 저장소 공통 규칙(Workflow 결정성, `make check`, 비밀 커밋 금지, 로컬 클라우드 프로필, 커밋·PR 규칙)
2. `docs/engineering/development-workflow.md`: 티켓 진행 절차. Provisioning용이지만 이 기능에도 그대로 준용한다
3. `docs/engineering/testing-strategy.md`
4. `specs/analysis-verification/spec.md`: 무엇을 만들어야 하는지(AV-001~AV-022, 수용 기준)
5. `specs/analysis-verification/plan.md`: 어떻게 만들지(모듈 위치, 처리 흐름, 규칙, Activity 설정, 결정 대기 표)
6. `specs/analysis-verification/tasks.md`: 티켓 목록·선행 관계·결정 대기
7. 이번에 맡은 `tickets/analysis-verification/<티켓>.md`
8. 이 문서의 §4 "재사용할 v2 모델"
9. 티켓이 Provisioning 경계를 건드리면 함께 읽는다
   - `specs/provisioning/input-contract-v2.md`: Planning 입력 v2와 **요구사항 JSON 형식**(구현 완료)
   - `specs/provisioning/onprem-shared-deployment.md`의 "v2 공통 렌더 입력·반환": Sandbox가 호출할 렌더 모듈(구현 완료)
   - `tickets/provisioning/b-01-image-build.md`: 공통 Build 모듈(`resource_id`·`source_version_id` 반영됨)
   - `docs/decisions/ADR-003.md`: 빌드 → 같은 Digest로 Sandbox 검증 → Production 재사용
   - `specs/provisioning/input-contract.md`(v1)와 `shared-contracts.md`는 역사적 기록이다. v1 모델은 새 정책의 증거로 쓰지 않는다

spec·plan·티켓은 main의 v2 구현(#22, #24, B-01 변경) 기준으로 맞춰져 있다. 문서와 코드가 다르면 **main의 코드**를 기준으로 삼고, 문서끼리 다르면 `spec.md` > `plan.md` > 티켓 순으로 따르며, 차이를 사용자에게 보고한다. 조용히 한쪽을 고르지 않는다.

## 2. 작업 범위

**만들어도 되는 곳**
- `packages/contracts/src/cmp_contracts/analysis.py`, `verification.py` (새 파일)
- `packages/scanners/`, `packages/agents/`(프롬프트 포함), `packages/testsuite/` (새 패키지. `pyproject.toml`의 `packages`·`package-dir`에 등록)
- `catalog/`, `skills/`
- `apps/worker/src/cmp_worker/activities/analysis.py`, `activities/sandbox.py`, `sandbox/`, `workflows/sandbox.py`, `workers/sandbox.py`
- `tests/analysis/`, `tests/agents/`, `tests/sandbox/`, `tests/integration/sandbox/`, `tests/fixtures/analysis/`
- `specs/analysis-verification/`, `tickets/analysis-verification/`

**건드리지 않는 곳 (민서 담당 Provisioning)**
- `cmp_contracts.provisioning`, `provisioning_v2`, `shared_deployment*`, `chart`, `application`, `plan`, `artifact`: 읽기·import만 한다. 수정하지 않는다
- `cmp_providers/*`, `cmp_worker/image_build.py`, `cmp_worker/provisioning*.py`, `infra/helm/*`, `infra/terraform/*`
- `specs/provisioning/`, `tickets/provisioning/`, ADR

**공유 파일 (Q-01 티켓에서만, 작게)**
- `cmp_contracts/queues.py`, `workers/run.py`, `workers/run_all.py`, `workflows/migration.py`
- 이 파일을 다른 티켓에서 바꿔야 하면 멈추고 사용자에게 알린다

Provisioning 쪽 모델이 부족하거나 바뀌어야 하면 구현하지 말고, 무엇이 왜 필요한지 정리해 보고한다.

## 3. 이미 정해진 결정 (바꾸지 않는다)

- 공통 렌더 모듈은 **렌더 전용**이다. Sandbox의 구체 값(Namespace, Release 이름, StorageClass, DB 비밀번호·앱 env Secret, 변수별 env 바인딩, NetworkPolicy)과 적용·정리는 **Sandbox 워커**가 정한다.
- 대상 정보는 Profile이 아니라 **capabilities**(아키텍처·StorageClass·이미지 접근)로 받는다. 빌드 플랫폼도 여기서 온다.
- 분석 결과(사용 프로필·매핑·사전 점검 등) 모델 이름은 **`AnalysisReport`**다. Provisioning v1의 `AnalysisResult`와 다르다.
- Enum 값은 **대문자**.
- 빌드는 Sandbox 밖에서 한다. 통과한 Digest를 그대로 Production에 넘기고 다시 빌드하지 않는다.
- 분석 결과와 요구사항은 **Sandbox 통과 시점의 최종 버전**으로 발행한다.
- Sandbox 통신 규칙: `network_intent`를 Sandbox NetworkPolicy로 바꾸고, Sandbox 전용 규칙(원본 클라우드 차단 등)은 별도로 얹는다. intent에 없는 외부 호출과 이전하는 서비스의 원본 주소는 차단·기록하고, 그 실패는 의도 누락(`SPEC`)으로 분류한다. 원본에 남겨 두는 서비스와 DNS는 허용한다.
- `.env` 실제 값은 Sandbox 워커가 Secret을 만들 때만 읽는다. 분석 단계는 이름만 다룬다.
- 테스트 묶음은 SHA-256 이름으로 S3에 저장하고 `run_suite(suite_ref, base_url, auth_ref, include)`로 실행한다. 테스트마다 `READ_ONLY` / `WRITES_DATA`를 표시한다. Production은 배포 직후 준비 검사만, 테스트 묶음은 데이터 이전 후 실행한다.
- 루프는 빌드 포함 최대 3회, 넘으면 사람 검토 대기. 어떤 경우에도 `finally`에서 Sandbox를 정리한다.
- 매핑 뒤 Sandbox 전에 **사용자가 필수 기능을 확정하고 잃는 것(`APPROVAL`)을 승인**한다(AV-022). 자동 승인은 없다. `BLOCK`이 있으면 Sandbox로 가지 않는다.
- 에이전트(Mapper·Tester·Fixer)는 `cmp_agents.runtime`의 도구 호출 루프로 돌고, 결과는 제출 도구로 스키마를 강제한다. Skill은 `skills/<name>/SKILL.md` 형식이고 필요한 것만 불러온다(plan §2.11).

## 4. 재사용할 v2 모델 (새로 정의하지 않는다)

| 용도 | 모델·함수 |
| --- | --- |
| 요구사항 출력 | `cmp_contracts.provisioning_v2.RequirementsV2`(`ApplicationRequirement`, `PostgresRequirement`, `NetworkIntent`, `ResourceOutputBinding`). JSON 바이트의 SHA-256이 `requirements_ref` |
| Planning 투영 | `cmp_contracts.provisioning_v2.PlanningRequestV2`(`AnalysisInputV2`, `BuildEvidenceV2`, `SandboxEvidenceV2`, `ValidatedWorkloadV2`, `S3ArchiveRef`). 검증은 `cmp_worker.provisioning_v2.prepare_plan_v2(envelope, requirements_bytes=...)` |
| Sandbox 렌더 | `cmp_contracts.shared_deployment_v2.SharedDeploymentRequestV2` → `cmp_providers.shared_deployment_v2.render_shared_deployment_v2(payload, database_chart_dir=..., application_chart_dir=...)` |
| Build | `cmp_worker.image_build.BuildContext`(`resource_id` 포함) → `ImageBuilder(context).build(source_uri, source_version_id=...)`. 반환 `image_uri`, `image_digest`, `source_ref`, `log`, `error_code` |

주의할 구현 사실:
- 요구사항의 PostgreSQL `extensions`가 비어 있지 않으면 Planning이 거절한다. 확장이 필요한 앱은 분석에서 `BLOCK`으로 보고한다(D-14).
- 요구사항에는 DB 이미지가 없다. 이미지는 대상 구성 단계에서 정한다(D-11). 그 전까지 Sandbox 개발은 공식 `postgres:16` Digest 고정값을 쓴다.
- `network_intent`는 출발지가 리소스여야 해서 외부 진입(인터넷 → 앱)을 표현할 수 없다. 외부 진입은 `AnalysisReport.external_entries`에 둔다(D-15).

## 5. 결정 대기 처리

`tasks.md`의 D-01~D-14 중 열린 항목에 티켓이 걸리면:

- 결정과 무관한 부분은 먼저 구현한다.
- 결정에 따라 달라지는 부분은 **인터페이스 뒤에 숨긴다**. 예: Build 호출은 `request_build` 하나로 감싼다.
- 결정 없이 진행할 수 없으면 티켓을 `BLOCKED`로 표시하고 필요한 결정을 적는다.
- 결정을 임의로 내리지 않는다. 특히 D-11(DB 이미지 선택), D-12(Build 호출 위치·시간 상한), D-14(확장), D-15(외부 진입 표현), Provisioning 모델 변경은 민서 담당이다. D-07(첫 사이클 지원 언어)은 사용자가 정한다.

## 6. 티켓 진행 절차

1. 작업은 최신 `main`에서 만든 새 브랜치에서 한다.
2. 티켓 상태를 확인한다. `DRAFT`면 사용자 검토 전이니 구현하지 말고 검토를 요청한다. 사용자가 승인하면 `TODO`로 바꾼다.
3. GitHub Issue를 찾아보고 없으면 만든다(티켓 링크, 관련 AV ID, 수용 기준, 선행 작업, Test Seam). 번호를 `tasks.md`에 적는다. 원격 작업 권한이 없으면 사용자에게 알린다.
4. 티켓의 **Test Seam을 사용자에게 확인받는다.** 무엇을 잡고 무엇을 못 잡는지 함께 말한다.
5. 관찰 가능한 동작 하나씩 RED → GREEN. 실패하는 테스트를 먼저 쓰고, 의도한 이유로 실패하는지 확인한 뒤 최소 구현.
6. `make check`(= `ruff check`, `ruff format --check`, `pytest`). Temporal을 바꿨으면 `make smoke-temporal`(로컬 인프라가 있을 때). HTTP를 바꿨으면 `make openapi`.
7. 구현 중 설계가 달라졌으면 `spec.md`·`plan.md`·티켓을 같은 작업 안에서 고친다. 바뀐 게 없으면 "문서 변경 없음"과 이유를 남긴다.
8. 티켓의 "검증 증거"에 명령·결과·미검증 항목을 적고, 수용 기준이 다 충족됐을 때만 `DONE`.
9. 커밋 메시지와 PR에 `#이슈번호`. push와 PR은 사용자가 허락했을 때만.

## 7. 지켜야 할 규칙

- **비밀:** `.env` 값, 자격 증명, DB 비밀번호를 로그·예외 메시지·Activity 인자·결과·Workflow 이력·LLM 입력·테스트 fixture에 넣지 않는다. fixture에는 가짜 값만 쓴다.
- **Workflow:** 외부 I/O·현재 시각·난수·LLM 호출은 Activity 안에서만. Workflow에는 참조와 작은 메타데이터만 둔다.
- **LLM:** 테스트에서는 실제 LLM을 부르지 않는다. 고정 응답 가짜 클라이언트를 쓴다. 실제 공급자는 D-01이 정해질 때까지 연결하지 않는다.
- **클라우드:** AGENTS.md의 로컬 프로필(`cmp-aws-prod`, `cmp-gcp-prod`)은 운영 이름의 환경이다. **사용자 승인 없이 쓰지 않는다.** 스캔은 읽기 전용 호출만, 단위 테스트는 `botocore.stub`이나 가짜 클라이언트로 한다.
- **클러스터:** Sandbox 통합 테스트는 별도 k3d 클러스터(`cmp-sandbox`)에서만 한다. 다른 클러스터(학습용 minikube 등)를 쓰거나 바꾸지 않는다.
- **커밋 금지:** `.env`, 자격 증명, 업로드 소스 아카이브, 생성된 비밀, Terraform plan/state.
- **기존 코드 재사용:** §4 표의 모델·함수를 복제하지 말고 import해서 쓴다.
- **포맷 검사:** CI의 `ruff format --check .`는 **Markdown 안의 ` ```python ` 코드 블록도 검사**한다. 문서에 Python 예시를 넣었으면 커밋 전에 `uv run ruff format specs/ tickets/`를 돌린다.
- **줄바꿈:** Windows 체크아웃은 CRLF일 수 있다. 줄바꿈만 다른 변경을 커밋하지 않는다.
- **오래된 문서:** 저장소 밖의 개인 작업 문서(`seungcheol-analysis-part.md`)는 최신 결정이 반영되지 않았다. 참고하지 않는다.

## 8. 시작할 티켓

외부 답이 필요 없는 순서: **01 → Q-01 → 02 → 02B → 03 → 04 → 08A → 09**. 05(D-07 지원 언어), 06(AWS 계정), 15(D-02 저장 경계)는 외부 입력이 필요하다.

## 9. 사용자에게 줄 시작 지시 예시

```text
specs/analysis-verification/agent-brief.md를 먼저 읽고, 거기 적힌 순서대로 문서를 읽은 다음
tickets/analysis-verification/01-contracts.md를 진행해 줘.
구현 전에 Test Seam을 나한테 확인받은 뒤 RED → GREEN으로 한 동작씩 진행해.
```
