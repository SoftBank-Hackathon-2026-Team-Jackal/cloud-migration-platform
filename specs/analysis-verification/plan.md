# Implementation plan: 분석·매핑·Sandbox 검증

[spec.md](spec.md)의 `AV-001`~`AV-021`을 Python 3.11, Pydantic v2, Temporal, Helm으로 구현하기 위한 **설계 제안**이다. 함수명·필드명·Activity 분할은 구현하며 확정한다. Provisioning과 맞물리는 계약은 [Provision 입력 계약](../provisioning/input-contract.md), [공유 계약](../provisioning/shared-contracts.md), [앱 렌더링](../provisioning/application-rendering.md), [B-01](../../tickets/provisioning/b-01-image-build.md)을 따른다.

## 1. Affected modules and contracts

```text
packages/contracts/src/cmp_contracts/
  analysis.py          # 새로: AnalysisRequest, UsageProfile, NetworkIntent, Evidence,
                       #       MappingRow, PrecheckReport, EnvClassification, AnalysisResult
  verification.py      # 새로: SourceArchive, BuildAttempt, SandboxAttempt, Verdict,
                       #       SandboxVerification (Planning SandboxResult v2로 투영)
packages/agents/src/cmp_agents/      # 새로: 에이전트 본체 (Temporal 의존 없음)
  llm.py  mapper.py  tester.py  fixer.py  masking.py
packages/scanners/src/cmp_scanners/  # 새로: 결정적 스캐너 (Temporal 의존 없음)
  source.py  code_python.py  manifests.py  aws_infra.py  network.py  profile.py  precheck.py
catalog/               # 카탈로그 YAML (사용 방식 어휘·탐지 규칙·등급 기준·표준 테스트)
skills/                # Skill 지침·보조 스크립트 (k8s-workload, db-postgres, network, config-env)
apps/worker/src/cmp_worker/
  activities/analysis.py      # 스캔·매핑 Activity (얇게 감싸기)
  activities/sandbox.py       # 새로: 묶음·빌드 호출·적용·테스트·판정·수정·정리 Activity
  sandbox/                    # 새로: Helm 적용·갱신·정리, 테스트 실행기, 출구 기록 수집
  workflows/sandbox.py        # 새로: SandboxVerificationWorkflow (자식)
  workers/analysis.py, workers/sandbox.py
tests/analysis/, tests/sandbox/, tests/fixtures/analysis/
```

- 새 Python 패키지는 `pyproject.toml`의 `packages`·`package-dir`에 등록한다.
- 기존 `cmp_contracts.provisioning.SandboxResult`·`BuildResult`(v1, 이전 순서)는 **수정하지 않는다**. v2 전환은 민서와 합의한 뒤 공동 작업으로 한다(§9).
- Queue: 기존 `analysis`를 쓰고, Sandbox는 새 `sandbox` Queue를 제안한다(`queues.py`에 추가). Build 모듈 호출 Activity도 `sandbox` Worker에서 실행하되 builder 권한(Docker·ECR push)은 그 Worker 프로세스에만 둔다. Worker를 나누지 않기로 하면 Queue 추가는 생략한다.

## 2. Design and data flow

```text
MigrationWorkflow (migration)
 ├─ analysis
 │   fetch_source ──┬─ scan_code ───────┐
 │                  ├─ scan_manifests ──┤ 병렬 (asyncio.gather)
 │                  ├─ scan_aws_infra ──┤
 │                  └─ scan_network ────┘
 │   build_profile → precheck → map_usage(LLM) → check_evidence
 │   (필수 기능 차단이면 여기서 중단·보고)
 ├─ SandboxVerificationWorkflow (자식, migration queue → sandbox activities)
 │   generate_tests(LLM, 별도 세션) → validate_tests → 고정(lock)
 │   package_source → request_build → BuildAttempt
 │   반복 attempt = 1..3:
 │     render_sandbox → apply_sandbox → readiness_check
 │     reset_test_data → run_tests → judge
 │     통과 → break
 │     실패 → classify_failure
 │        SPEC   → fix_mapping(LLM) → (다음 시도: 재render, 같은 Digest)
 │        CODE/BUILD → fix_code(LLM) → package_source → request_build → (새 Digest)
 │        RENDER → 중단, Provisioning 담당 보고
 │   3회 초과 → 사람 검토 Signal 대기
 │   finally: teardown_sandbox
 └─ AnalysisResult + SandboxVerification → Planning 입력 조립(민서)
```

- 자식 Workflow로 분리하는 이유: 독립 취소·정리(finally teardown), 시도별 이력 구분, 사람 검토 대기. temporal-flow.md §7의 분리 근거(별도 취소·정리)에 해당한다.
- Workflow에는 참조와 작은 메타데이터만 둔다. 스캔 원본·렌더 결과·테스트 보고서는 S3에 `<sha256>` 이름으로 저장하고 `ContentRef`로 넘긴다.

### 2.1 소스 확보 (AV-002)

1. 개발 중: 토큰 클론. 이후: GitHub App(해당 저장소 읽기, 짧은 수명 토큰).
2. 운영 커밋: EKS의 실행 이미지 → ECR 이미지 설정의 OCI 라벨에서 커밋. 실패 시 사용자 지정 커밋 + `commit_verified=false`.
3. 작업 공간: Worker의 격리 디렉터리 `<workdir>/<migration_id>/<commit>`. 원본(빌드용)과 마스킹본(LLM용)을 분리 보관한다.

### 2.2 스캔 (AV-003~AV-006)

| 스캐너 | 방식 | 결과 |
| --- | --- | --- |
| `code_python` | 표준 `ast`: `boto3.client/resource(...)` 호출과 메서드, `os.environ`/`getenv`, `psycopg`/`sqlalchemy` import·URL | 호출 지점 목록(파일·줄) |
| `manifests` | YAML 파싱, Helm 차트는 `helm template` 후 | 리소스별 클라우드 의존(어노테이션·StorageClass·SA) |
| `aws_infra` | boto3 읽기 전용(EKS, EC2 VPC/Subnet/RouteTable/SG, ELBv2, IAM role policy, CloudTrail LookupEvents) | 리소스 설정 |
| `network` | SG·Ingress → `NetworkIntent{flows, egress, exposure, source_cidrs}` | 네트워크 의도 |
| `profile` | 위 결과 병합 + Dockerfile 파싱(`COPY`/`ADD` 경로 존재 확인) | `UsageProfile` |
| `precheck` | 원본 DB 읽기 전용 쿼리(가능할 때) 또는 마이그레이션 파일 정적 분석 | `PrecheckReport` |

교차 해석: 코드 + CloudTrail 일치 = 확실히 사용, 코드에만 = 드문 경로, 권한에만 = 미사용 권한(거버넌스 항목). CloudTrail은 S3 데이터 이벤트를 기본 기록하지 않는다는 한계를 결과에 표시한다.

자격 증명은 `get_session(credential_ref)` 인터페이스 뒤에 둔다. 개발 중에는 SSO 프로필, 이후 환경 연결(민서)이 발급한 참조로 교체한다. 개발용 역할에도 스캔용 정책(읽기 전용, 비밀 값 조회 금지)을 그대로 붙이고 그 정책을 권한 목록으로 전달한다.

### 2.3 매핑 (AV-007, AV-008)

- 카탈로그 항목: `capability`, `usage_vocabulary`, `detect_rules`, `grade_rules`, `known_differences`, `standard_tests`.
- Mapper 입력: 마스킹된 증거 요약 + 해당 Skill 지침 + 카탈로그 후보. 출력: `MappingRow[]`(JSON 스키마 강제).
- 처리 표(기계적): 직접 대응·API 다름 → 진행. 부분 대응 → 필수면 대체 방법 증명 시 진행 아니면 차단, 선택이면 승인. 대응 없음 → 필수면 차단, 선택이면 승인.
- `check_evidence`: 근거의 파일·줄 존재, 매니페스트 경로, 리소스 ID 조회. 실패한 행은 `UNVERIFIED`로 둔다.

### 2.4 소스 묶음 (AV-009)

```python
def build_archive(root: Path, files: list[Path]) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for p in sorted(files):
                info = tar.gettarinfo(root / p, arcname=str(p))
                info.mtime = 0; info.uid = info.gid = 0; info.uname = info.gname = ""
                with open(root / p, "rb") as f:
                    tar.addfile(info, f)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()
```

- 파일 목록: `.dockerignore` 적용 후 `.git/`, `.env*`, 인증 파일 패턴 제외. 일반 파일만 넣는다. B-01이 심볼릭 링크·경로 이탈·`.env`를 `UNSAFE_ARCHIVE`로 거절하므로 같은 규칙을 묶기 전에 미리 검사한다.
- Dockerfile이 하위 폴더에 있으면 컨텍스트 범위를 정해 루트로 재배치한다. Dockerfile이 없으면 Fixer가 생성(코드 수정과 같은 취급).
- 업로드 전 `HeadObject`로 같은 키가 있으면 생략한다.

### 2.5 Build 모듈 연계 (AV-010)

```python
context = BuildContext.model_validate({
    "migration_id": migration_id,
    "attempt_id": f"{run_id}-b{n}",        # 코드 변경마다 새 n, 같은 빌드 재시도는 같은 n
    "ecr_repository": settings.ecr_repository,
    "platform": target_platform,          # 대상 선택에서 결정
    "log_bucket": settings.build_log_bucket,
})
outcome = ImageBuilder(context).build(source_uri)   # Activity 안에서만 호출
```

- **재사용은 호출 측에서**: `(source_sha256, platform) → BuildOutcome`을 실행 기록에 남기고, 같은 키면 Build 모듈을 부르지 않는다. B-01은 같은 `attempt_id` 재시도만 재사용하고 `reused` 필드는 없다.
- 실패 코드 분류(B-01 구현 기준):

| 코드 | 분류 | 처리 |
| --- | --- | --- |
| `DOCKERFILE_MISSING` | 수정 대상 | Fixer가 Dockerfile 생성 → 새 묶음·새 시도 |
| `EXTERNAL_COMMAND_FAILED` | 모호(다운로드·로그인·build·push 구분 불가) | 같은 `attempt_id`로 1회 재시도 → 또 실패하면 빌드 실패로 보고 Fixer, 근거 없음 표시 |
| `SOURCE_METADATA_INVALID`, `SOURCE_DIGEST_MISMATCH`, `INVALID_ARCHIVE`, `UNSAFE_ARCHIVE`, `ARCHIVE_TOO_LARGE` | 묶음 문제 | 묶음 규칙 확인·재패키징. Fixer 대상 아님 |
| `ECR_*`, `BUILD_DIGEST_*`, `LOG_UPLOAD_*` | 인프라 | Activity 재시도 후 사람 검토 |

- **결정 필요**: Fixer가 빌드 실패를 고치려면 실패 단계와 정제된 출력 일부가 필요하다. 현재 B-01 로그는 상태·코드만 남긴다. `BUILD_FAILED`/`PUSH_FAILED` 분리와 정제된 마지막 N줄 제공을 민서에게 요청한다. 그 전까지 코드 수정 시연은 런타임 실패(테스트 실패) 사례로 한다.

### 2.6 Sandbox 렌더·적용·갱신 (AV-011)

현재 쓸 수 있는 공통 기능과 빈 곳:

| 필요 | 현재 | 첫 사이클 처리 |
| --- | --- | --- |
| DB 렌더 | `PostgresChartValues` + `infra/helm/postgres` | 그대로 사용, `environment=SANDBOX` |
| 앱 렌더 | `prepare_application` → `resolve_application` → `render_application` + `infra/helm/application` | 그대로 사용, DB host·secret_name 동적 바인딩 |
| Namespace·Release·Secret 이름 규칙 | 없음(입력으로 받음) | 임시 규칙 `cmp-sbx-<run>`, `<ns>-db`, `<ns>-app`. 통합 함수 제공 시 교체 |
| DB Secret | Chart가 만들지 않음(기존 Secret 참조) | Sandbox Worker가 가짜 비밀번호 Secret 생성(값은 Worker 안에서만) |
| 내부 Ingress, NetworkPolicy | 렌더 안 함 | **결정 필요**. 첫 사이클은 Ingress·NetworkPolicy 검증 제외로 시작하고 민서 Chart 추가를 기다림 |
| 인프라 준비 검사 | 없음 | 임시 구현(Pod Ready, `pg_isready`+SQL, 헬스 GET). 공통 함수 제공 시 교체 |
| 템플릿 버전 | Chart `version` | 렌더에 쓴 Chart 이름·버전과 Values SHA-256을 시도 기록에 남김 |

갱신 규칙:

| 바뀐 것 | 동작 |
| --- | --- |
| 이미지 Digest만 | 앱 Release만 `helm upgrade` (롤링) |
| 앱 설정·바인딩 | 앱 Values 재렌더 → `helm upgrade` |
| DB 설정(바꿀 수 있는 값) | DB Release `helm upgrade` |
| 바꿀 수 없는 필드, DB 메이저 버전, 손상 | 해당 Release 삭제 + PVC 삭제 → 재설치 |
| 테스트 데이터 | 매 시도 `DROP DATABASE`·재생성 또는 합성 데이터 재주입 |

- DB Chart의 PVC는 `helm.sh/resource-policy: keep`이라 `helm uninstall`로 지워지지 않는다. 정리는 Namespace 삭제로 한다.
- 모든 리소스에 `cmp.run-id`, `cmp.expires-at` 라벨. 주기 정리 작업이 만료된 Namespace를 지운다.
- 개발 환경: 04번처럼 별도 k3d 클러스터(`cmp-sandbox`)를 쓴다. minikube는 쓰지 않는다.

### 2.7 테스트 (AV-012, AV-013)

- 테스트 정의: YAML 시나리오(`request` → `expect`) + 필요 시 Python 검사. 실행기는 `base_url`·인증 참조만 주입받아 Sandbox와 Production에서 같은 정의를 돌린다.
- 표준 테스트(첫 사이클): 헬스체크, 앱 API로 DB 쓰기 → 읽기 → 내용 일치, 재시작 후 데이터 유지(PVC).
- AI 테스트 검증(`validate_tests`):
  1. 원본 쪽 테스트 자원에서 통과(프로덕션 원본에서 실행 금지).
  2. 상태 코드가 아닌 결과 값 assertion이 있는지 정적 검사.
  3. 대상 의존성(DB Service, 외부 호스트)을 NetworkPolicy·프록시로 끊고 실행 → 실패해야 함.
  4. 통과한 테스트 묶음을 `sha256`으로 고정. 이후 Fixer 입력에는 읽기 전용으로만 제공하고, 실행 시 해시를 다시 확인한다.
- 출구 기록: Sandbox Namespace의 기본 egress를 출구 프록시로만 허용(NetworkPolicy가 들어온 뒤). 프록시 접근 로그에서 목록 밖 호스트를 수집한다.
- 실행 커버리지: Python 앱이면 `coverage run`으로 감싼 테스트 이미지 변형이 필요해 **첫 사이클 제외**, 호출 지점별 테스트 매핑으로 대체 표시.

### 2.8 판정과 실패 분류 (AV-014, AV-015)

- `judge`는 결정적 함수: 필수 테스트 결과, 앱 로그의 미지원 호출 패턴, 출구 기록, 매핑 대조 → `Verdict{passed, reasons[]}`.
- `classify_failure` 순서: (1) 렌더 결과가 요구사항과 필드 단위로 다르면 `RENDER` (2) 빌드 실패면 `BUILD` (3) 테스트 실패 + 스택트레이스가 앱 코드면 `CODE` (4) 나머지 `SPEC`. 판단이 애매하면 Fixer가 원인 후보를 제안하고, 판정은 다음 시도의 결과로 확인한다.

### 2.9 에이전트 (Mapper·Tester·Fixer)

| 역할 | 세션 | 도구 | 금지 |
| --- | --- | --- | --- |
| Mapper | A | 증거 조회, 카탈로그·Skill 읽기 | 비밀 값, 원본 쓰기 |
| Tester | B(분리) | 사용 프로필·API 목록 읽기 | Fixer 수정 내용 보기 |
| Fixer | A | 작업 공간 diff 제안, 로그·보고서 읽기, 요구사항 패치 제안 | 테스트 수정, Production 자격 증명 |

- 에이전트끼리 대화하지 않는다. Workflow가 결과물만 넘긴다.
- 모든 출력은 Pydantic 스키마로 검증한다. 판단 과정(본 증거, 시도한 수정)은 보고서에 남겨 승인 화면에 제공한다.
- LLM 클라이언트는 `cmp_agents.llm` 하나로 감싸고 공급자는 ADR로 정한다. 테스트에서는 고정 응답 가짜 클라이언트를 쓴다.

### 2.10 코드 색인과 조회 도구 (후순위, 티켓 17)

첫 사이클 이후, 스캐너 결과를 코드 색인(심볼·호출 그래프·환경변수 읽기·라우트)으로 저장하고 에이전트가 `find_calls`·`callers_of`·`env_readers`·`routes`·`snippet`·`resource_graph`로 조회하게 한다. 파일 단위 입력 대신 근거 주변 조각만 LLM에 보내고, 도구가 돌려준 위치를 그대로 `Evidence.locator`로 쓴다. 파일 해시별 캐시로 Fixer 수정 후 바뀐 파일만 다시 파싱한다. 벡터 검색은 쓰지 않는다. 분석 내부 구현이라 외부 계약은 바뀌지 않는다.

## 3. 계약 초안

```python
class Evidence(Contract):
    kind: Literal["CODE", "MANIFEST", "CLOUD_RESOURCE", "IAM_POLICY", "CLOUDTRAIL", "CONFIG"]
    locator: str            # 파일:줄, 매니페스트 경로, 리소스 ID, 이벤트 ID
    verified: bool = False  # check_evidence 결과

class MappingRow(Contract):
    capability: str
    usage: str
    grade: Literal["DIRECT", "API_DIFF", "PARTIAL", "NONE"]
    required: bool
    loses: list[str]
    treatment: Literal["PROCEED", "APPROVAL", "BLOCK"]
    status: Literal["UNVERIFIED", "EVIDENCE_CHECKED", "SANDBOX_VERIFIED"]
    evidence: list[Evidence]

class SandboxVerification(Contract):          # Planning SandboxResult v2로 투영
    migration_id: str
    ref: VersionRef
    status: Literal["PASSED", "FAILED", "NEEDS_REVIEW", "STOPPED_RENDER"]
    build_ref: VersionRef                    # 검증한 BuildAttempt
    image_uri: str                           # repo@sha256:...
    image_digest: str
    platform: str
    source: ContentRef                       # 소스 묶음 sha256
    config_ref: VersionRef
    requirements_ref: VersionRef
    target_selection_ref: VersionRef
    template_version: str
    report_ref: ContentRef
    test_suite_ref: ContentRef
    attempts: list[SandboxAttemptSummary]
    cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]
```

Planning 입력과의 대응은 민서에게 보낸 계약안(Planning 입력의 `sandbox.*`, `build.*`)과 같은 이름을 쓴다. 대조 규칙: `build.image_digest = sandbox.image_digest`, 소스 참조 일치(analysis·build·sandbox), `policy.template_version = sandbox.template_version`, `sandbox.status = PASSED`.

## 4. Activity 경계와 실행 설정

| Activity | Queue | timeout | 재시도 | 비고 |
| --- | --- | --- | --- | --- |
| `fetch_source` | analysis | 2분 | 3(네트워크만) | 같은 커밋 재사용 |
| `scan_*` | analysis | 3분 | 3(스로틀링만) | Heartbeat. 권한 거부는 비재시도 오류 |
| `build_profile`, `precheck`, `check_evidence` | analysis | 1분 | 2 | 결정적 |
| `map_usage`, `generate_tests`, `fix_*` | analysis / sandbox | 2분 | 2 | 스키마 검증 실패도 재시도 |
| `validate_tests` | sandbox | 5분 | 1 | 원본 테스트 자원 필요 |
| `package_source` | sandbox | 2분 | 2 | 같은 해시면 업로드 생략 |
| `request_build` | sandbox | 15분 | 1(+분류 규칙) | Heartbeat. 실패는 결과값으로 반환 |
| `render_sandbox`, `apply_sandbox` | sandbox | 5분 | 2 | `helm upgrade --install --wait` |
| `readiness_check`, `reset_test_data` | sandbox | 2분 | 2 | |
| `run_tests` | sandbox | 3분 | 1 | 테스트 실패는 결과값 |
| `judge`, `classify_failure` | sandbox | 30초 | 2 | 결정적 |
| `teardown_sandbox` | sandbox | 3분 | 5 | 취소·실패에도 실행, TTL 이중 정리 |

수치는 측정 후 조정한다. 비재시도 오류는 `ApplicationError(non_retryable=True)`로 구분한다.

## 5. Failure, retry, and rollback behavior

| 실패 | 처리 |
| --- | --- |
| 클론 실패·권한 없음 | 비재시도, 사용자 조치 요청 |
| 스캔 권한 거부 | 비재시도, 필요한 권한 이름 반환 |
| LLM 출력 스키마 오류 | 2회 재시도 후 사람 검토 |
| 근거 확인 실패 | 해당 행 `UNVERIFIED`, 진행은 처리 규칙에 따름 |
| 필수 기능 대응 없음 | 분석 단계에서 차단, Sandbox로 가지 않음 |
| 빌드 실패 | §2.5 분류 |
| Helm 적용 실패 | Release 상태 조회 → 같은 Values로 재시도, 바꿀 수 없는 필드면 재생성 분기 |
| 준비 검사 실패 | 테스트 없이 실패 판정, 원인 분류로 |
| 테스트 실패 | 결과값, 수정 루프 |
| 렌더 결과 불일치 | 루프 중단, Provisioning 담당 보고 |
| 3회 초과 | 사람 검토 Signal 대기(타이머 상한), 정리 실행 |
| Worker 장애·취소 | `finally` 정리 + TTL 라벨 정리 작업 |

Production 롤백은 이 기능 범위가 아니다. Sandbox는 정리가 곧 원상 복구다.

## 6. 보안

- 마스킹(`cmp_agents.masking`): 고엔트로피 문자열, 알려진 키 패턴(AKIA…, `-----BEGIN`), `.env` 값 위치. 마스킹본만 LLM에 보낸다.
- `.env`는 이름만 파싱해 보관하고 값은 읽은 즉시 버린다. Sandbox에는 가짜 값을 넣는다.
- Sandbox Worker 권한: Sandbox 클러스터의 지정 Namespace 패턴만, Build 호출용 S3 쓰기·Builder 실행. Production 자격 증명 없음.
- 로그에는 참조·해시·오류 코드만 남긴다. Helm·Docker 원시 출력은 공개 오류에 복사하지 않는다(민서 03과 같은 원칙).

## 7. Verification

| 대상 | 방법 | 증명하지 못하는 것 |
| --- | --- | --- |
| 계약 | Pydantic 모델 테스트, fixture JSON 왕복, Planning 입력 대조 함수 | 상대 Worker의 실제 출력 |
| 스캐너 | 데모 앱 축소 fixture(코드·매니페스트) 결과 스냅샷, AWS는 `botocore.stub` | 실제 계정의 권한·지연 |
| 매핑 | 가짜 LLM 응답으로 스키마·처리 표·근거 확인 검사 | 실제 모델의 판단 품질 |
| 소스 묶음 | 같은 입력 두 번 → 같은 해시, 제외 규칙, B-01 `_safe_extract` 통과 | 실제 S3 권한 |
| Sandbox | 로컬 k3d + 실제 Helm 3.17.3, 포트 오류 주입 → 수정 → 통과 | GKE/EKS Sandbox 차이 |
| Workflow | Temporal 테스트 환경(time skipping)으로 루프·3회 초과·취소 정리, `make smoke-temporal` | 실제 외부 시간 |

`make check`는 Python 변경마다, HTTP 변경 시 `make openapi`.

## 8. 요구사항 대응표

| 요구사항 | 설계 위치 | 티켓 |
| --- | --- | --- |
| AV-001 | §1, §3 | 01, 02 |
| AV-002 | §2.1 | 03 |
| AV-003 | §2.2, §2.10 | 05, 17 |
| AV-004 | §2.2 | 04 |
| AV-005 | §2.2 | 06 |
| AV-006 | §2.2 | 07 |
| AV-007, AV-008 | §2.3, §2.9, §2.10 | 08, 17 |
| AV-009 | §2.4 | 09 |
| AV-010 | §2.5 | 10 |
| AV-011 | §2.6 | 11 |
| AV-012, AV-013 | §2.7 | 12, 13 |
| AV-014, AV-015 | §2.8 | 13, 14 |
| AV-016 | §2.6, §5 | 11 |
| AV-017 | §3 | 01, 07 |
| AV-018 | §3 | 15 |
| AV-019 | §2.1 | 14 |
| AV-020 | §6 | 전 티켓 |
| AV-021 | §4, §5 | 02, 전 티켓 |

## 9. 구현 전 결정이 필요한 사항

| 항목 | 권장 방향 | 담당 |
| --- | --- | --- |
| Planning `SandboxResult`/`BuildResult` v2 | §3 필드로 v2 추가, v1 유지. 민서 통합 작업과 같은 PR 또는 연속 PR | 민서·승철 |
| 통합 Sandbox 렌더 함수, Ingress·NetworkPolicy 렌더 | 앱 Chart에 내부 Ingress·NetworkPolicy 옵션 추가 요청 | 민서 |
| Build 실패 상세 | `BUILD_FAILED`/`PUSH_FAILED` 분리 + 정제된 출력 일부 | 민서 |
| `sandbox` Queue 추가 | builder 권한 격리를 위해 추가 | 승철(민서 공유) |
| LLM 공급자·모델·전송 범위 | ADR-004로 기록 | 승철·팀 |
| 소스 묶음 버킷·정리 규칙 | 플랫폼 소유 버킷, 실행 종료 후 N일 | 민서·승철 |
| 데모 앱 언어·DB 형태 | 스캐너 범위 결정 | 소울 |
