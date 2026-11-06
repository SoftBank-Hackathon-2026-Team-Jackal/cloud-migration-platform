# Tasks: 분석·매핑·Sandbox 검증

이 파일은 분석·검증 티켓의 **인덱스**다. 각 티켓의 수용 기준·Test Seam·결정 대기는 연결된 파일에 있다. 진행 방식은 [개발 워크플로](../../docs/engineering/development-workflow.md)를 이 기능에도 준용한다: 상태는 `DRAFT → TODO → IN_PROGRESS → DONE`, 막히면 `BLOCKED`. 사용자 검토 후 `TODO`로 바꾸고, 구현 전에 GitHub Issue를 만들거나 연결해 번호를 아래에 적는다. 한 번에 한 티켓씩 진행한다.

| ID·티켓 | 상태 | Blocked by | 관련 요구사항 | GitHub Issue | 완료 |
| --- | --- | --- | --- | --- | --- |
| [01 분석·검증 계약 모델](../../tickets/analysis-verification/01-contracts.md) | DONE (PR #31 병합) | 없음 | AV-001, AV-017, AV-018, AV-020, AV-022 | #29 | ☑ |
| [Q-01 `sandbox` Task Queue·Worker 등록](../../tickets/analysis-verification/q-01-sandbox-queue.md) | DONE (PR #33 병합) | 없음 (D-05 결정) | AV-021 | #32 | ☑ |
| [02 분석 Activity 골격](../../tickets/analysis-verification/02-temporal-skeleton.md) | DONE (PR #46 병합) | 01 | AV-001, AV-021 | #44 | ☑ |
| [02B Sandbox 자식 Workflow 골격](../../tickets/analysis-verification/02b-sandbox-workflow.md) | DONE (PR #46 병합) | 02, Q-01 | AV-014, AV-015, AV-016, AV-021 | #45 | ☑ |
| [03 소스 확보·운영 커밋 고정·마스킹](../../tickets/analysis-verification/03-source-fetch.md) | DONE (main 반영) | 01 | AV-002, AV-020 | #48 | ☑ |
| [04 매니페스트·클러스터 리소스 스캔](../../tickets/analysis-verification/04-manifest-scan.md) | DONE (PR #54 병합) | 01 | AV-004 | #50 | ☑ |
| [05 코드 스캔과 `.env` 대조](../../tickets/analysis-verification/05-code-scan.md) | DONE (PR #58 병합) | 03, D-07(언어별 어댑터만) | AV-003 | #57 | ☑ |
| [06 AWS 인프라·네트워크 스캔과 통신 의도](../../tickets/analysis-verification/06-aws-infra-scan.md) | IN_PROGRESS (PR #90 병합, 실제 계정 실행 남음) | 01, AWS 테스트 계정(외부) | AV-005, AV-020 | #89 | ☐ |
| [06B 확정된 분석 범위를 분석 입력으로 연결](../../tickets/analysis-verification/06b-scope-input.md) | DONE (PR #237 병합, 미확인: 실제 클러스터 후보 실행, 웹 `scope_id` 계약 합의) | 06, 티켓 56(병합) | AV-001, AV-005, AV-020 | #232 | ☑ |
| [06C 앱의 AWS 리소스 사용 추적(선택·비선택 후보 대조, CloudTrail 근거)](../../tickets/analysis-verification/06c-usage-tracing.md) | IN_PROGRESS (구현·자동 테스트 완료, 실제 계정 확인 남음) | 06, 06B (병합됨) | AV-003, AV-005, AV-007, AV-017, AV-020 | 미발행(사용자 발행 후 기록) | ☐ |
| [07 요구사항·사용 프로필·사전 점검 조립](../../tickets/analysis-verification/07-profile-precheck.md) | DONE (PR #64, #77, #86 병합) | 04, 05 (06 결과가 없으면 인프라 항목을 "미수집"으로 표시) | AV-006, AV-017 | #59 | ☑ |
| [08A 에이전트 실행 기반과 Skill 형식](../../tickets/analysis-verification/08a-agent-runtime.md) | DONE (PR #68 병합) | 01 | AV-007, AV-020 | #67 | ☑ |
| [08 카탈로그·Skill v1과 매핑](../../tickets/analysis-verification/08-mapping.md) | DONE (PR #73 병합) | 07, 08A, D-01(실제 LLM 연결 시) | AV-007, AV-008, AV-020 | #72 | ☑ |
| [08B 필수 기능 확정과 손실 승인 게이트](../../tickets/analysis-verification/08b-review-gate.md) | DONE (PR #80 병합) | 02, 08 | AV-022, AV-008 | #78 | ☑ |
| [09 소스 묶음 생성과 S3 업로드](../../tickets/analysis-verification/09-source-archive.md) | DONE (PR #84 병합) | 03 | AV-009, AV-020 | #83 | ☑ |
| [10 Build 모듈 연동과 실패 분류](../../tickets/analysis-verification/10-build-integration.md) | DONE (PR #88 병합) | 09, Provisioning B-01(DONE) | AV-010 | #87 | ☑ |
| [11 Sandbox 구성·적용·정리](../../tickets/analysis-verification/11-sandbox-apply.md) | DONE (PR #94 병합) | 02B, 07, Provisioning 공통 렌더 v2(#24, DONE) | AV-011, AV-016, AV-020 | #93 | ☑ |
| [11B Sandbox 제자리 갱신과 테스트 데이터 초기화](../../tickets/analysis-verification/11b-sandbox-update.md) | DONE (PR #96 병합) | 11 | AV-011, AV-015 | #95 | ☑ |
| [12 테스트 묶음·Tester·`run_suite`](../../tickets/analysis-verification/12-test-suite.md) | DONE (PR #101 병합) | 07, 08A, 11 | AV-012, AV-020 | #97 | ☑ |
| [13 통신 규칙·판정·실행 기록](../../tickets/analysis-verification/13-judge-telemetry.md) | DONE (PR #103 병합) | 08, 11, 12 | AV-013, AV-014 | #102 | ☑ |
| [14A 실패 분류와 Fixer](../../tickets/analysis-verification/14a-classify-fixer.md) | DONE (PR #107 병합) | 08, 10, 11B, 13 | AV-015, AV-017 | #106 | ☑ |
| [14B 갱신 루프와 부모 종료 상태](../../tickets/analysis-verification/14b-fix-loop.md) | DONE (PR #110 병합) | 14A | AV-015, AV-019, AV-020 | #109 | ☑ |
| [14C 최종 발행·재승인·수정 패치 산출물](../../tickets/analysis-verification/14c-publish.md) | DONE (PR #112 병합) | 14B | AV-017, AV-019, AV-020 | #111 | ☑ |
| [15 Planning 인계(입력 v2)](../../tickets/analysis-verification/15-planning-handoff.md) | DONE (PR #142 병합, 미확인: 등록 후 실제 계획 게시) | 14C, 21, 22, 25 | AV-018 | #141 | ☑ |
| [16 데모 앱 통합 검증](../../tickets/analysis-verification/16-demo-e2e.md) | DRAFT | 06, 15, 데모 앱 준비 | AV-001~AV-022, spec §7 | 미생성 | ☐ |
| [17 코드 색인과 에이전트 조회 도구](../../tickets/analysis-verification/17-code-index.md) | DRAFT(후순위) | 05, 08 | AV-003, AV-007, AV-020 | 미생성 | ☐ |
| [18 공개 GitHub 저장소 선택 결과의 분석 입력 연결](../../tickets/analysis-verification/18-public-github-source.md) | TODO (담당자 계약 조율 전) | 02, 03, #56 병합 | AV-001, AV-002, AV-020 | #60 | ☐ |
| [19 분석 Activity 통합](../../tickets/analysis-verification/19-analysis-integration.md) | DONE (PR #92 병합) | 03~08B, 06 | AV-001~AV-008, AV-017, AV-020, AV-021 | #91 | ☑ |
| [20 Claude 클라이언트와 비용 상한](../../tickets/analysis-verification/20-claude-client.md) | DONE (PR #114 병합) | 08A, 08, 12, 14A, ADR-004 | AV-007, AV-012, AV-015, AV-020 | #113 | ☑ |
| [21 분석 → Sandbox 루프 → 발행 연결과 통합 테스트](../../tickets/analysis-verification/21-pipeline-wiring.md) | DONE (PR #119 병합) | 14C, 19, 20 | AV-001, AV-015, AV-017, AV-018 | #115 | ☑ |
| [22 Sandbox 워커 실제 조립과 실행 준비](../../tickets/analysis-verification/22-worker-wiring.md) | DONE (PR #121 병합) | 14B, 20, 21 | AV-001, AV-015, AV-021 | #120 | ☑ |
| [23 비공개 소스 접근(토큰 참조·교체 가능한 해석기)](../../tickets/analysis-verification/23-source-access.md) | DONE (PR #132 병합) | 03, 19, 21 | AV-001, AV-002, AV-020 | #131 | ☑ |
| [24 실행 도구·입력 검증·만료 Sandbox 정리·Tester 연결](../../tickets/analysis-verification/24-ops-and-tester.md) | DONE | 12, 14B, 22 (D-09는 인터페이스만) | AV-011, AV-012, AV-020 | #136 | ☑ |
| [25 빌드 연결(소스 S3 업로드 → Build 모듈 → 빌드 증거)](../../tickets/analysis-verification/25-build-wiring.md) | DONE (PR #142 병합, 미확인: 실제 AWS 실행) | 09, 10, 22 (실제 AWS는 D-06·D-12·승인) | AV-009, AV-010, AV-018 | #140 | ☑ |
| 분석 웹 이벤트 연결(데모, Provisioning 쪽 작업) — 티켓 파일 없음, 상세는 [이벤트 안내서 §2 분석](../../docs/engineering/events-guide.md) | IN_REVIEW (PR 대기, 박승철님 확인 필요) | 19, Provisioning 24(이벤트 저장소) | AV-001, AV-020 | #226 | ☐ |
| [ENV-05 분석: env_file_ref에서 키 읽기·EnvPlan 출력](../../tickets/env-file-ref/05-analysis-env-plan.md) | DONE (구현 완료, PR 대기) | ENV-02, ENV-03 (병합됨) | AV-001, AV-020 | #197 | ☐ |
| [ENV-06 Sandbox: render_env로 앱 Secret 만들기](../../tickets/env-file-ref/06-sandbox-render-env.md) | DONE (구현 완료, PR 대기) | ENV-03, ENV-05 | AV-011, AV-020 | #197 | ☐ |
| [26 Sandbox의 비공개 ECR 이미지 pull](../../tickets/analysis-verification/26-sandbox-ecr-pull.md) | DONE (구현 완료, PR 대기, 실제 토큰은 승인 후) | 25, Provisioning 29 (실제 토큰은 승인 필요) | AV-010, AV-011, AV-020 | #197 | ☐ |
| [27 표준 테스트를 앱 API 모양에 맞추기](../../tickets/analysis-verification/27-standard-test-shapes.md) | IN_PROGRESS (27-A 구현·k3d 확인 완료, 27-B 구현 완료·실제 Claude와 k3d 확인 대기) | 12, 22, 24 (D-09 재정의 결정됨) | AV-012 | #199 | ☐ |
| [28 보안 측정 모델과 원본 측정(데모 4행)](../../tickets/analysis-verification/28-security-measure.md) | IN_PROGRESS (28-A·28-B 구현·자동 테스트 완료, 실제 계정 확인·템플릿 반영 남음) | 04, 06, 07, 19 (병합됨) | AV-023 | 미생성 | ☐ |
| [29 보안 동등 판정·약점 표시·승인 게이트 연결](../../tickets/analysis-verification/29-security-judge.md) | IN_PROGRESS (29-A 판정·표시 구현·자동 테스트 완료, 29-B 승인 연결은 D-17·상헌 합의 후) | 28, 08B, D-17(정책) | AV-023, AV-022 | 미생성 | ☐ |
| [30 Sandbox 보안 프로브](../../tickets/analysis-verification/30-security-probes.md) | DRAFT | 12, 13, 24, 28, D-18 | AV-023, AV-012, AV-013 | 미생성 | ☐ |
| [31 Fixer가 상황에 맞는 Skill을 필요할 때 불러 쓰게 연결](../../tickets/analysis-verification/31-fixer-skills.md) | IN_PROGRESS (구현·자동 테스트 완료, 실제 Claude 확인 남음) | 14A, 20 (병합됨) | AV-007, AV-015, AV-020 | 미발행(사용자 발행 후 기록) | ☐ |

## 선행 관계

```text
01 → 02 ─┐
Q-01 ────┴→ 02B ─┐
01 → 03 → 05 ─┐  │
01 → 04 ──────┴→ 07 ─┬→ 08 → 08B (02 필요)
01 → 08A ────────────┘ │ (08은 08A 필요)
              06 ┄┄→ 07 (선택 입력)
03 → 09 → 10           │
02 + 03 → 18 (공개 GitHub 소스 입력; #56 병합·담당자 계약 확인)
02B + 07 → 11 → 11B    │
03~08B + 06 → 19 (분석 Activity 통합)
08A + 08 + 12 + 14A + ADR-004 → 20 (Claude 연결·비용 상한)
14C + 19 + 20 → 21 (분석 → Sandbox 루프 → 발행 연결) → 22 (워커 실제 조립·실행 준비) → 23 (비공개 소스), 24 (실행 도구·Tester)
09 + 10 + 22 → 25 (빌드 연결) → 15 (Planning 인계)
07 + 08A + 11 → 12
08 + 11 + 12 → 13
08 + 08B + 10 + 11B + 13 → 14A → 14B → 14C → 21 → 15 → 16
06 → 16
05 + 08 → 17 (후순위)
```

정확한 선행 조건은 위 표와 각 티켓을 따른다. D-05는 Sandbox 전용 Queue를 추가하기로 결정했다. 외부 의존: 06은 AWS 테스트 계정, 08은 D-01(실제 LLM 연결 시), 10은 B-01(완료)과 D-12, 11은 공통 렌더 v2(완료)와 D-11, 15는 D-02, 16은 데모 앱·Sandbox 기반(D-08).

**추천 순서:** 01 → Q-01 → 02 → 02B → 03 → 04 → 05 → 07 → 08A → 08 → 08B → 09 → 10 → 11 → 11B → 12 → 13 → 14A → 14B → 14C → 19 → 20 → 21 → 22 → 23 → 24 → 25 → 15 → (06) → 16 → 17. 외부 의존이 없는 것부터 이어 첫 사이클 뼈대(스캔 → 빌드 → Sandbox 루프)를 먼저 완성한다. 17은 첫 사이클이 끝난 뒤 착수한다. 18은 02·03 구현을 재사용하되 #56 병합과 분석 담당자 계약 확인 후 별도로 진행한다.

## 결정 대기

| ID | 내용 | 영향 티켓 | 담당 |
| --- | --- | --- | --- |
| D-01 | 결정됨 — Claude, Anthropic SDK(Messages API 도구 호출), 모델 `claude-sonnet-5-5`, 비용 상한은 코드로 강제(ADR-004, 티켓 20) | 08, 12, 14, 17 | 승철·팀 |
| D-02 | 결정됨(승철) — 첫 사이클의 `*_ref`는 해시 이름 로컬 불변 저장소(`LocalArtifactStore` 관례, S3는 후속). 분석·Build 결과의 저장·정규화는 분석 쪽이 맡음. **조사 결과(2026-11-05):** Planning(`plan_publish`)은 요구사항 바이트를 자기 저장소(`api_settings.artifact_store_dir`)에서 `<root>/<sha256>`로 읽고 `ArtifactRef`의 필드(id·version·kind·sha256·size_bytes)도 같으므로, `ContentRef`류(요구사항·보고서·검토 기록)는 두 쪽이 같은 디렉터리를 가리키면 된다(단일 호스트). `S3ArchiveRef`류(소스 묶음·테스트 묶음)는 계약이 S3 URI를 요구하므로 실제 S3 업로드가 필요하다 | 10, 15, 19, 21 | 승철 |
| D-04 | Build 실패 상세(`BUILD_FAILED`/`PUSH_FAILED` 분리, 정제된 출력 일부) — 급하지 않음 | 10, 14 | 민서 |
| D-05 | 결정됨 — `sandbox` Queue 분리 | Q-01, 02B | 승철(민서 공유) |
| D-06 | 소스·테스트 묶음 버킷 소유·이름·정리 규칙, Sandbox 이미지 보존 기간. 실제 빌드(D-12)와 15번 인계 전에 필요. 계정은 `cmp-aws-prod`(운영 이름)라 생성·사용 전에 사용자 승인이 필요하다. **현재 플랫폼의 S3 버킷은 두 개뿐이다**(플랫폼 구성도, 2026-11-05): Terraform State·잠금 버킷과, GitHub Actions가 선택한 커밋의 소스를 올려 EC2가 내려받는 배포 버킷. 둘 다 분석의 소스·테스트 묶음·빌드 로그용이 아니므로, 새 버킷을 만들지 배포 버킷에 접두사(`sources/`, `tests/`)를 쓸지 정해야 한다. **임시 방향(승철, 2026-11-05):** 새 버킷 없이 기존 버킷을 같이 쓰고 키 접두사로 구분한다(코드는 이미 `sources/`·`tests/`). Terraform State 버킷은 쓰지 않는다. **민서님 확인(2026-11-06):** 배포 버킷을 소스·테스트 묶음·빌드 로그용으로 같이 쓴다. 구현됨([Provisioning 28](../../tickets/provisioning/28-build-registry-runtime.md), `infra/demo/build-stack.yaml`): 배포 버킷 `sources/`·`tests/`·`builds/` 읽기·쓰기(`releases/`는 그대로)이며, 새 스택 배포와 서버 반영은 승인 대기 | 09, 10, 12, 15, 25 | 민서·승철 |
| D-07 | 결정됨 — 첫 사이클 지원 언어는 Go(데모 앱 `demo-app`이 Go) | 05, 16, 17 | 승철 |
| D-08 | 결정됨(승철) — Sandbox 기반은 이 Mac의 로컬 k3d `cmp-sandbox`(Colima, 전용 kubeconfig)로 준비. 설정 방법은 11 티켓의 환경 준비 참조 | 11, 16 | 승철 |
| D-09 | AI 테스트 1번 조건용 원본 쪽 테스트 자원 (**재정의됨 2026-11-06**: 필수가 아니다. 속성 테스트는 소스 근거 검사, 기록 테스트는 기준 상태 앱의 응답 기록으로 채택하고, 모델이 정한 리터럴 기대값만 원본 자원이 있어야 쓴다. 원본 자원은 있으면 추가 검증. [27-B](../../tickets/analysis-verification/27-standard-test-shapes.md)) | 12 | 승철·소울 |
| D-10 | Production 테스트 묶음 실행 시점(데이터 이전 후·트래픽 전환 전)·데이터 정리 | 15 | 민서·소울·상헌 |
| D-11 | DB 이미지를 고르는 "대상 구성 단계"의 소유자, Sandbox·Production 공유 방식(요구사항엔 이미지 없음, #22) | 11 | 민서 |
| D-12 | Build 호출 Worker(권한 배치)·전체 시간 상한. 이미지를 올리는 ECR은 플랫폼 쪽 데모 ECR이다(도메인 경계 문서의 "Registry(데모 ECR)"), 사용자 계정의 ECR이 아니다. **민서님 구현(Provisioning 28, 2026-11-06):** 플랫폼 ECR 저장소 `cmp-platform-builds`(태그 불변)와 push 권한을 새 스택 `cmp-demo-build`에 두고, 별도 컨테이너 대신 worker에 호스트 Docker 소켓과 Docker CLI·buildx를 넣었으며 `BUILD_*` 설정은 서버 `.env`에서 읽는다(우리 설정 이름과 같다). 서버 반영은 승인 대기. **남은 간극:** 우리 `sbx_build`는 Sandbox 루프가 설정된 워커(지금은 k3d가 있는 이 컴퓨터)에서만 돌아서, 서버 worker의 빌드 환경을 쓰려면 빌드를 서버로 보내는 연결이 따로 필요하다(`resource_id`·`source_version_id`는 구현됨). 코드 연결은 25에서 끝났고(`BUILD_*` 설정이 모두 있을 때 켜짐) 실제 AWS 실행이 이 결정과 승인을 기다린다 | 10, 25 | 민서 |
| D-13 | 결정됨 — DB 내부 상태 사전 점검(wal_level, 슬롯, 기본 키 없는 테이블 등)은 데이터 이전의 `dm_precheck`가 맡는다(`specs/data-migration`, 소울). 분석의 07은 정보 제공에 그친다 | 07 | 소울 |
| D-14 | PostgreSQL 확장 지원 시점과 `CREATE EXTENSION` 실행 주체(현재 확장 요구는 Planning이 거절) | 07 | 민서 |
| D-15 | 외부 진입(인터넷 → 앱)을 `network_intent`로 표현하는 방법(그 전까지 `AnalysisReport`에 기록) | 01, 06, 13 | 민서 |
| D-16 | 결정됨, V2·렌더러 구현 완료(#79, #81), 07 바인딩 반영 — DB 연결을 단일 URL 변수로 받는 앱은 `env_bindings`에 `connection_url_secret_ref` 출력으로 바인딩. Sandbox와 운영이 각각 URL을 만들어 Secret에 저장하고 앱에는 Secret 이름·키만 주입. 형식(URL 인코딩, Secret 키 이름)은 민서님과 확정 | 07, 11 | 민서(V2·공통 렌더러·Secret 주입)·승철(07 바인딩, 11 검증) |
| D-17 | 보안 동등성 정책: "원본 약점을 승인하면 대상에서 고쳐서 배포"를 제품 정책으로 인정할지(`policies.md` 변경 필요), 보강의 대상 구성 반영 방법(Provisioning 계약) | 29, 후속 | 사용자·민서
| D-18 | DB SSL 강제를 Sandbox·대상에서 구현하는 방식(이미지 인증서·설정 공급 주체)과 운영용 API 판별 기준 | 28, 30 | 승철·민서

## 변경 기록

- 2026-11-06: 27-B 구현(#199): Tester가 소스(라우트 목록, 읽기 전용 소스 조각 도구, `test-authoring` 스킬)를 읽고 쓰기 모양(`shapes`)과 읽기 요청(`probes`)을 소스 근거와 함께 낸다(기대값은 쓰지 않음). 쓰기 모양은 왕복·재시작 후 유지 속성 테스트가 되고, 읽기 요청은 기준 상태 앱에 두 번 보내 같은 응답이면 기록 테스트(`ai-0rec-`)가 된다. `validate_tests` 1번 조건을 "원본 자원에서 통과"에서 "속성 테스트 또는 기록 테스트 + 소스 근거 확인, 모델이 정한 리터럴 기대값만 원본 자원"으로 바꿨다(D-09 재정의). 원본 자원이 없어도 Tester가 호출된다. 근거·기록 필드는 없으면 바이트에서 빠져 기존 묶음 해시가 그대로다. Workflow 변경 없음(활동 안의 변경). 가짜 모델·서버로 검증했고 실제 Claude·k3d 확인은 비용 승인 후 남았다.
- 2026-11-06: 27(#199) 구현·확인. 표준 테스트가 앱의 쓰기 API 모양(쓰기 본문, 성공 상태, 응답 ID 위치, 읽기 방식)을 설정으로 받고, 쓰기가 멱등인 앱(Go 데모 앱: 서버가 ID를 만들어 201, 같은 키 재전송은 200과 저장된 행)은 같은 쓰기를 다시 보내 같은 ID·본문이 오는지로 DB 저장을, DB 재시작 뒤 같은 ID가 오는지로 유지를 검증한다. 같은 실행의 케이스가 같은 키를 쓰면 멱등 쓰기의 첫 쓰기가 200이 되는 문제를 케이스별 키로 고쳤다. 재시작 직후 앱이 503(같은 ID로 재시도하라)을 주는 것을 실제 k3d에서 발견해 재시작 케이스의 다음 요청을 5xx·무응답일 때만(최대 60초) 다시 보내게 했고(`ActionStep.recover_seconds`), 테스트별 실패 사유 코드를 Sandbox 결과에 싣는다. Go 코드의 HTTP 라우트 스캐너(`cmp_scanners.routes`)와 분석 문서의 `http_routes`를 추가했다(Tester의 입력 후보). 실제 k3d에서 `--sandbox`로 모양을 주고 세 표준 테스트 통과(Claude 없이). 후보 모양 중에서 자동으로 고르는 단계(2층)는 만들었다가 제거했다: 후보가 앱 하나뿐이라 일반성이 없고 Workflow 변경이 따라와서, 일반화는 소스 근거 기반 AI 테스트(스킬, D-09)로 간다.
- 2026-11-06: **실제 Claude로 코드 수정 경로 확인(`CODE`, 재빌드).** 헬스 핸들러에 `nil` 맵 쓰기 버그를 넣은 Go 앱 사본으로 실행: 시도 1 `CODE` 실패(앱 로그의 패닉 스택이 `main.go`를 가리킴) → Fixer가 `main.go`를 읽고 버그 두 줄만 제거 → 새 Digest로 재빌드(`sbx_build` 시도 2, 이미지 Digest가 달라짐) → 시도 2 통과, 정리 완료. 발행된 패치는 `git diff` 그대로 두 줄 삭제이고 적용 후 소스 트리 해시가 건강한 원본의 해시와 같다(패치가 원본을 정확히 복원). 패치는 브랜치에 적용하거나 push하지 않고 산출물로만 남음. 이 실행의 모델 비용 약 0.115달러(오늘 합계 약 0.150달러). 아직 못 본 것: 매핑 행을 바꾸는 수정(재승인 게이트), DB 쓰기·읽기 테스트.
- 2026-11-06: **실제 Claude로 Fixer 경로 첫 확인(요구사항 수정).** 매니페스트 포트를 9090으로 틀리게 한 Go 앱 사본으로 실행: Claude 매핑 5행(제안대로 모두 필수로 확정, 모두 `PROCEED`) → 빌드·Sandbox → 시도 1 `SPEC` 실패(`SANDBOX:CLUSTER_COMMAND_FAILED`) → Fixer가 앱 로그 `listening on :8080`을 근거로 요구사항 포트 9090→8080만 고침(재빌드 없음, 헬스 경로는 그대로) → 시도 2 통과, 정리 완료, 수정된 요구사항으로 발행. 총 모델 비용 약 0.035달러(매핑 0.026 + Fixer 0.009). 관찰: 매핑의 `db-runtime` 사용 설명이 "백그라운드 작업자"로 어긋남. 코드 수정 경로(`CODE`)는 장애 B로 감지·분류까지만 확인했고 Fixer 호출은 하지 않았다.
- 2026-11-06: **실제 Go 앱으로 전체 루프 첫 통과(Claude 없이, 1단계).** 입력은 Go `demo-app` 소스와 Go에 맞춰 새로 쓴 매니페스트(`k8s/source.yaml`: `DATABASE_URL`·`ENV_NAME`·`ADDR`, 헬스 `/healthz`). 분석은 pgx와 환경변수 5개를 찾고 `DATABASE_URL`을 연결 URL 대체로 묶었으며, 실제 빌드(S3 업로드 → ECR push)와 k3d Sandbox(DB·앱 기동, 비공개 ECR pull)를 거쳐 `std-health` 통과, 첫 시도 PASSED, 정리 완료, 발행. 매핑은 Claude를 껐으므로 행 0개. 확인하지 못한 것: 실제 Claude 매핑과 Fixer, 매핑 행이 있는 검토 게이트, DB 쓰기·읽기 테스트(Go 앱의 쓰기 API 모양이 표준 테스트와 다름).
- 2026-11-06: 개인 계정(S3 버킷·프라이빗 ECR 저장소 각 1개)으로 실제 빌드 경로를 처음 확인: 소스 묶음 S3 업로드(버전 ID 포함) → 빌드 모듈 → ECR push → Digest가 ECR 조회와 일치, 같은 소스는 재업로드·재빌드 없이 재사용. 이 컴퓨터(Mac, Colima)에서는 임시 `DOCKER_CONFIG` 때문에 buildx와 Docker 컨텍스트를 못 찾아 `DOCKER_HOST` 지정과 플러그인 경로 처리가 필요했다(runbook). 같은 날 Sandbox(k3d)의 비공개 ECR pull도 확인: 우리 `apply_pull_secret`이 만든 `cmp-ecr-pull`이 있으면 이미지를 받고(`Successfully pulled`), 없는 대조군은 `ErrImagePull`·`ImagePullBackOff`였다. 임시 네임스페이스는 지워졌다. 아직 확인 못 한 것: 실제 Go 앱 이미지로 전체 루프.
- 2026-11-06: Provisioning 44(#179): 웹 시작 요청(`target`·`source`·`env_file_ref`)이 CLI `build_start_input`과 같은 시작 입력(`analysis`·`planning`·`env_file_ref`)을 만든다. `PlanningSettings`는 운영자 대상 Profile 카탈로그에서 나오며, Planning 인계(15)·Workflow 코드는 바뀌지 않았다. D-15는 영향 없음.
- 2026-11-06: ENV-05·06·26 구현(#197): 분석이 `env_file_ref`에서 키 이름만 읽어 `EnvPlan`을 만들고(없으면 기존 저장소 `.env*` 방식), Sandbox가 `render_env`로 앱 Secret·`<DB>-conn` Secret을 만들며(`env_file` 경로는 호환용), Planning 인계에 `env_plan`을 넣고 `PlanningSettings.env_file_ref`는 선택으로 바꿨다. Sandbox는 비공개 플랫폼 ECR 이미지를 위해 단기 토큰 pull Secret을 만든다(`cmp_providers.ecr_pull` 재사용). 분석 워커와 Sandbox 워커에 `.env` 저장소 설정이 필요하다.
- 2026-11-06: 최신 main 점검(`make check` 1955 통과). `.env` 인계(ENV-01~04 병합, ENV-05·06이 우리 몫): 부모 Workflow가 시작 입력의 `env_file_ref`를 분석·Sandbox·Planning 입력에 복사한다(`env-file-ref-v1`). 민서님 Provisioning 28(빌드 스택, worker Docker)·29(ECR pull Secret 공용 모듈 `cmp_providers.ecr_pull`)·26(NetworkPolicy 변환 공용 `cmp_providers.network_policy`, Sandbox가 이미 사용)·데이터 이전(소울)이 병합됨. D-06·D-12·D-13 갱신.
- 2026-11-06: 민서님 답변 반영. D-06: 배포 버킷 접두사 공유와 쓰기 권한 추가 확정(구현 대기). D-12: 빌드 컨테이너를 따로 추가하고 ECR push 권한을 함께 주는 방향(구현 대기, 호출 방식은 설계 후 협의). EKS는 같은 계정이면 노드 역할로 ECR 이미지를 받고, 그 밖의 대상은 배포 때 권한을 만들어 붙인다(민서님). 우리 몫으로 남는 것: Sandbox(k3d)가 비공개 ECR 이미지를 받는 pull Secret(GKE 쪽 방식처럼 네임스페이스 Secret과 `default` ServiceAccount 연결).

- 2026-11-05: 플랫폼 구성도 대조로 S3 서술을 바로잡음: 소스·테스트 묶음 키는 `sources/<sha256>.tar.gz`·`tests/<sha256>.tar.gz`(코드 기준, 문서의 `<migration_id>/` 경로는 틀렸음), D-06에 현재 버킷 두 개가 분석용이 아님을 기록. 배포된 플랫폼(EC2, Docker Compose)의 워커는 Sandbox 클러스터 설정이 없어 scaffold만 실행하므로 실제 루프는 k3d가 있는 컴퓨터의 워커로 돌린다(runbook).
- 2026-11-05: `make smoke-temporal` 통과(로컬 Temporal 1.29.1, `scaffold_complete`). 스크립트가 Sandbox 자식 Workflow 도입(02B) 이후 `sandbox` 워커를 띄우지 않아 승인 대기에 못 닿던 문제를 고쳤다. 이 컴퓨터에는 `docker compose` 플러그인이 없어 `make infra-up` 대신 같은 이미지(Temporal 자동 설정 서버와 전용 DB)를 `docker run`으로 띄워 확인했다. 이 확인은 문자열(scaffold) 경로만 다루며, 새 인계 경로(`--planning`)와 실제 빌드는 시간 건너뛰기 환경의 Workflow 테스트로 확인했다.
- 2026-11-05: 25(#140)·15(#141) 구현: 빌드 설정 네 항목이 모두 있으면 소스 S3 업로드 → 민서님 Build 모듈 → `BuildAttempt` 증거(`builds_ref`)를 쓰는 실제 빌드 경로(없으면 미리 빌드한 이미지 유지, 빌드 실패는 `BUILD:<코드>`로 사람 검토), 테스트 묶음 S3 아카이브. 통과한 실행은 `sbx_plan_assemble`로 Planning 입력을 조립·검증하고 `register_planning_input`(민서님 등록 함수 호출)으로 등록한 뒤 계획 단계로 간다(`planning-handoff-v1` patched, `--planning` 설정이 있을 때만). 실제 AWS 실행은 D-06·D-12·승인 대기. 부모 Workflow 변경은 호출 1곳과 헬퍼 1개.
- 2026-11-05: 24 구현(#136): `scripts/sandbox_ctl.py`(`status`/`cleanup`, 전용 컨텍스트만)와 소스 입력 검증(`-` 시작·공백·제어 문자 거절, git 호출 전), 만료 Sandbox 정리 진입점(주기 실행은 cron·launchd 안내), Tester 연결(`sbx_ai_tests`, 설정 `ai_tests`, 원본 실행기가 없으면 모델 미호출, Workflow는 `ai-tests-v1` patched). 원본 쪽 테스트 자원은 D-09 대기.
- 2026-11-05: 정리. 23을 DONE(PR #132)으로 맞춤. 코드 수정 후 push·PR 생성(`DraftPrPublisher`의 GitHub 구현)은 후순위로 확정: 지금은 패치 산출물만 반환하고 쓰기 토큰은 쓰지 않는다. `.env` 단일 참조 작업(`specs/env-file-ref`)의 ENV-05(분석)는 분석 담당, ENV-06(Sandbox)은 Sandbox 담당 몫이다. 그 전까지 Sandbox의 `env_names`·`env_file` 설정은 임시 경로다.
- 2026-11-05: 10 구현: `request_build`가 B-01 Build 모듈을 `builder_factory`로 감싸 호출하고(D-12 대비), `(resource_id, source_sha256, platform)`으로 성공 결과를 재사용하며, 실패 코드를 분류하고 `BuildEvidenceV2`로 정규화한다.
- 2026-11-05: D-16 구현 반영: 민서님이 V2에 `connection_url_secret_ref`를 추가(#81, Secret `<DB release>-url`의 키 `url`)했고, 07이 `*_URL`·`*_URI` 변수를 이 출력으로 바인딩한다(`DATABASE_URL`은 `replaced`). key=value DSN은 계속 보고만 한다. 11은 `cmp_providers.database_url`의 두 함수로 같은 형식의 URL Secret을 만든다.
- 2026-11-05: 09 구현: `package_source`로 원본 작업 공간에서 재현 가능한 소스 아카이브를 만들어 `sources/<sha256>.tar.gz`로 올린다(같은 키는 업로드 생략). B-01 아카이브 검사를 통과한다.
- 2026-11-05: 08B 구현: `AnalysisWorkflow`에 확정·승인 Signal과 검토 Query를 두고 `ReviewGate`로 처리 표를 적용. 차단은 자식 실패(`ANALYSIS_BLOCKED`)로 멈춘다.
- 2026-11-05: D-16 결정: 단일 URL 연결 변수는 `connection_url_secret_ref` 출력으로 바인딩한다(민서님이 V2·공통 렌더러·Secret 주입을 구현한 뒤 07이 바인딩을 만들고 11이 같은 방식으로 검증). 그 전까지 07은 `DB_CONNECTION_URL_VARIABLE`로 표현 불가 보고만 한다. DSN(key=value) 형식은 이번에 지원하지 않는다.
- 2026-11-05: 04·07 보강(Issue #75): 실제 원본 매니페스트로 NetworkPolicy 통신 의도, ConfigMap·secretKeyRef 변수 이름 수집, 클러스터 밖 DB 보고를 추가. 첫 사이클의 원본 PostgreSQL은 RDS가 아니라 클러스터 안 Pod로 결정.
- 2026-11-05: D-01 일부 결정: LLM 공급자는 Claude이고 Anthropic SDK(Messages API 도구 호출)를 쓴다(Agent SDK는 자체 도구·루프가 08A의 허용 목록 정책과 겹쳐 쓰지 않는다). 모델·코드 전송 범위·키 관리·비용 한도는 ADR-004에서 정한다. 실제 연결은 08에서 `LLMClient` 뒤에 어댑터로 붙인다.
- 2026-11-05: 04(PR #54, Issue #50)와 05(PR #58, Issue #57, Go 어댑터 포함) 완료. 둘 다 fixture 기준 검증이며 실제 EKS 매니페스트·실제 Helm 렌더는 검증하지 못함.
- 2026-11-05: 공개 GitHub 소스 선택을 기존 분석 입력에 연결하는 Ticket 18/Issue #60 등록. 구현 방식은 분석 담당자와 조율 후 확정한다.
- 2026-11-05: D-07 결정: 첫 사이클 지원 언어는 Go. 05의 언어 어댑터를 Go로 구현한다(데모 앱 저장소 `demo-app`은 Go 1.24, 의존성은 pgx).
- 2026-11-05: 03 완료(Issue #48). 브랜치 upstream 설정 때문에 PR 없이 main에 직접 반영됐으며 push CI는 통과.
- 2026-11-05: 02·02B를 PR #46으로 병합(이슈 #44, #45 종료). 14·15의 부모 Workflow 변경 범위를 plan에 기록하고 14에 종료 상태 정리 수용 기준을 추가.
- 2026-11-05: 06 구현(가짜 AWS 응답 기준): `scan_aws_infra`가 허용 목록의 읽기 전용 호출만으로 EKS·VPC·보안 그룹·ELBv2·IRSA 정책·CloudTrail 요약을 읽고, `build_network`가 보안 그룹·코드 외부 호스트를 `network_intent`로, 로드 밸런서를 외부 진입·VPC CIDR로 바꾼다. `boto3`를 런타임 의존성으로 추가. 실제 계정 1회 실행(승인 필요)은 남음.
- 2026-11-05: D-02 결정: 분석·Build 결과의 `*_ref` 저장은 해시 이름 로컬 불변 저장소(민서님의 `LocalArtifactStore`와 같은 관례, S3는 후속)로 하고 저장·정규화는 분석 쪽이 맡는다. 02~08B·06의 "Activity 연결은 후속"을 모아 Ticket 19(분석 Activity 통합)를 추가.
- 2026-11-05: 19 구현(가짜 LLM·AWS 미설정 기준): `AnalysisPipeline`과 `AnalysisActivities`가 03~08B·06의 실제 함수를 연결하고 결과를 `ArtifactStore`에 참조로 저장한다. `AnalysisWorkflow`는 문자열 입력이면 scaffold, `AnalysisInput` dict면 실제 경로를 탄다. 부모의 입력 연결과 실제 Claude 연결은 남음.
- 2026-11-05: 11 구현: `SandboxService`가 네임스페이스·Secret·`helm upgrade --install --wait`·준비 검사·정리를 맡고, 실제 k3d `cmp-sandbox`(D-08 결정: 로컬 Colima + k3d)에서 DB 16과 공개 앱 이미지로 끝까지 검증. Helm 3 설치로 전체 테스트를 로컬에서 통과(937). Workflow Activity 연결은 14에서.
- 2026-11-05: 11B 구현: `plan_update`가 리소스별 갱신 동작(UNCHANGED/UPGRADED/RESTARTED/RECREATED)을 정하고 `update_sandbox`가 필요한 Release만 갱신한다. env Secret은 내용 해시 어노테이션으로 변경을 감지해 `rollout restart`로 반영, 테스트 데이터는 DB Pod 안 `pg_dump`/`pg_restore` 기준선으로 초기화. 실제 k3d에서 검증, RECREATED 경로는 단위 테스트만.
- 2026-11-05: 12 구현: `packages/testsuite`(시나리오 형식·결정적 묶음·`run_suite` 해시 거부·표준 테스트·`validate_tests` 네 조건)와 Tester(`generate_tests`, Fixer 입력 없음). 묶음 저장은 D-02 로컬 저장소(S3 후속), 조건 3은 DB scale 0, 원본 실행기는 D-09 대기로 인터페이스+가짜. 실제 k3d에서 검증.
- 2026-11-05: 13 구현: 기본 차단 NetworkPolicy·DNS·`network_intent` 항목·허용 목록 출구 프록시(`apply_network`, 기록 `egress_records`, 외부 차단 `external_cut`)와 순수 함수 `judge`, `covers` 기반 `coverage_report`. 프록시는 Sandbox 워커 소유로 확정. 실제 k3d에서 검증.
- 2026-11-05: 티켓 14를 14A(분류·Fixer)·14B(갱신 루프·종료 상태)·14C(발행·재승인·패치 산출물)로 분할. GitHub App은 쓰지 않기로 하고 Draft PR은 패치 산출물로 바꿈. 14A 구현: `classify_failure`(INFRA→RENDER→BUILD→SPEC→CODE→SPEC), 렌더 필드 비교, Fixer(요구사항·intent 수정, 코드 수정, 테스트·`.env` 수정 거절, INFRA·RENDER 모델 호출 금지).
- 2026-11-05: 14B 구현(#109): `SandboxVerificationWorkflow`에 실제 갱신 루프 추가(문자열 입력은 scaffold 그대로, dict 입력이 실제 경로, `sbx_*` Activity). 검토 대기는 종료 상태가 아닌 부모 플래그(`review_pending`)로, 실패는 `failed` 하나(원인 `failure_code`)로 정리(`sandbox-terminal-v1` 패치). SPEC 경로는 실제 k3d로 검증, 나머지는 가짜 Activity 이력. 부모→Sandbox 자식 입력 연결과 API SSE 종료 조건(`failed`) 반영은 남음.
- 2026-11-05: 14C 구현: 통과 시 최종 분석 버전 발행(`SANDBOX_VERIFIED`, 새 버전 해시), 처리가 바뀐 행만 `ReapprovalGate`로 재승인, 코드 수정은 실제 git 패치 산출물(적용 결과 해시 검증, 전송 없음)로 반환. 재승인 Signal·Query(`approve_changed_rows`, `reapproval_state`)를 API에 연결하는 일은 남음.
- 2026-11-05: ADR-004와 티켓 20: Claude Sonnet 5.5 연결(`AnthropicLLMClient`)과 비용 상한(호출 전 예약·정산, 마이그레이션 $3 / 하루 $20 / 한 달 $100 기본, 차단 스위치, fallbacks 미사용). 기본은 꺼짐이며 키·장부·설정이 모두 있어야 켜진다. 실제 호출 1회 확인은 승인 후.
- 2026-11-05: 21 구현: 부모가 dict 입력이면 분석 결과로 Sandbox 입력을 조립(`assemble_sandbox_input`)해 실제 루프를 시작하고 통과 결과를 Query `pipeline`에 둔다(문자열 입력·기존 이력 그대로). 고정된 표준 헬스 테스트는 대상의 헬스 경로를 실행 시 받도록 보정. 실제 k3d에서 분석 → 검토 → 루프(SPEC 실패 후 정정) → 발행을 한 Workflow로 검증. 병합된 11~20 티켓의 상태·이슈 번호와 spec·plan·agent-brief의 낡은 내용(AV-019 패치 산출물, 저장소, 라벨 이름 등)도 함께 정리.
- 2026-11-05: 22 구현: 배포된 Sandbox 워커가 설정(`SANDBOX_KUBECONFIG`, 분석 저장소)이 있을 때 실제 루프를 등록(`build_sandbox_loop`), 앱 이미지는 Digest 고정 목록으로 임시 주입(`PrebuiltImages`, D-12 전), 시작 스크립트·사전 점검·실행 안내(runbook) 추가. 개발 전용 로컬 저장소 허용 설정은 기본 꺼짐.
- 2026-11-05: 23 구현: 분석 입력에 토큰 참조(`source_access_ref`)를 추가하고, 토큰은 교체 가능한 해석기(`SourceTokenResolver`)로만 푼다. 지금은 개발용 `env`(워커 환경 변수 `CMP_GITHUB_TOKEN`), GitHub 로그인이 붙으면 해석기만 교체. 토큰은 `https://github.com/<owner>/<repo>`에만 보내고 명령줄·이력·로그·결과에 남기지 않는다. 티켓 18의 "플랫폼은 토큰을 받지 않는다" 전제를 "토큰은 플랫폼이 보관, 분석은 참조로만 사용"으로 변경.
- 2026-11-04(팀 확인): 민서님이 분석·Sandbox 자식 Workflow 분리 방향에 이견이 없다고 확인. 새 종료 상태(`needs_review`, `stopped_render`)는 기존 완료·취소에 끼워 맞추지 않고 검토 대기·실패·완료를 구분해 API(SSE 종료 조건)와 함께 정리하기로 함(API는 민서님 담당). 자식에 직접 Signal을 보내는 설계와 권한·상태 이중 확인도 동의. 신규 Worker 배포 시 동작 이상 여부는 별도 확인 항목.
- 2026-11-04(리뷰 반영): spec AV-012에 `run_suite`의 `include`, AV-015에 INFRA와 횟수 규칙 반영(AV-013 커버리지는 원문 유지). ADR-004는 작성 예정으로 표시.
- 2026-11-04: D-05 결정: `sandbox` Task Queue를 분리한다. Q-01에서 Worker 등록, 02·02B에서 범위를 한정한 Workflow 연결을 수행한다.
- 2026-11-04: 분석을 고정 ID `analysis-<migration_id>`의 자식 Workflow로 분리하고, 검토 Signal은 상태 Query 후 자식에 직접 보낸다. Q-01은 Temporal 테스트 환경을 기본 기동 검증으로 사용한다.
- 2026-11-04(목적 점검): 원래 목적(필수 기능을 잃지 않고 옮김) 대비 빠진 단계를 보완. AV-022(필수 기능 확정·손실 승인)와 08B 게이트, 에이전트 실행 기반·Skill 형식(08A, plan §2.11) 추가. 14에 변경 행 재승인과 매핑 행 `SANDBOX_VERIFIED` 갱신 추가.
- 2026-11-04(v2 동기화): main에 병합된 v2 구현(#22 Planning 입력 v2·`RequirementsV2`, #24 공통 렌더 v2, B-01 `resource_id`·`source_version_id`) 기준으로 spec·plan·티켓을 맞춤. 요구사항은 `RequirementsV2` 재사용, DB 이미지는 대상 구성 단계(D-11), 확장 요구는 차단 보고(D-14), 외부 진입 표현은 D-15로 분리. D-07을 '첫 사이클 지원 언어(승철 결정)'로 변경.
- 2026-11-04: 민서님 계약 협의안 v2 반영(리소스 목록, capabilities, 변수별 env_bindings, validated_workloads, 배포 후 테스트 시점). 호출부 결정 방식에 따라 Sandbox 구체 값·Secret·NetworkPolicy를 Sandbox 워커 책임으로 명시. 리뷰 피드백으로 02→02·02B, 11→11·11B 분할, Q-01 신설, 01 완료 조건 축소, AnalysisReport 이름 변경. D-11~D-14 추가.

## 후속(첫 사이클 이후, 티켓 미작성)

- 후순위(필수 아님): 수정 패치를 원본 레포에 push하고 PR을 여는 기능. 같은 `SourceTokenResolver`를 `WRITE`로 쓰는 `DraftPrPublisher`의 GitHub 구현이다. 지금은 패치 산출물을 사람이 적용한다.
- `.env` 단일 참조 전환(`specs/env-file-ref`의 ENV-05·ENV-06·ENV-07): 분석이 `env_file_ref`에서 키 이름을 읽어 EnvPlan을 내고, Sandbox가 공통 `render_env`를 쓴다. 완료되면 `SandboxSettings.env_names`·`env_file` 임시 경로를 걷어낸다.
- Sandbox 앱에 주입되는 값의 화면 노출(바뀐 DB 호스트·포트·이름·사용자): 지금은 준비 단계에서만 계산되고 결과에 담기지 않는다. 프론트가 필요하다고 확인되면 결과에 읽기 자리를 추가한다(비밀번호·연결 URL은 Secret 참조뿐이라 제외).
- S3: `storage-s3` 카탈로그·Skill, 엔드포인트·인증 코드 수정 → 같은 루프. presigned URL 사례. 첫 사이클에서는 `KEEP`.
- Dockerfile 없는 앱의 Fixer 생성 고도화, 운영 이미지 재사용 대체 경로.
- 실행 커버리지 계측 이미지, 내부 Ingress 경로 검증(차트 지원 후).
- 대상 나란히 비교(GCP vs 온프레미스), 놓친 의존성 자동 반영, 매핑 정확도 측정.
