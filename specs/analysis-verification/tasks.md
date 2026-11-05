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
| [07 요구사항·사용 프로필·사전 점검 조립](../../tickets/analysis-verification/07-profile-precheck.md) | DONE (PR #64, #77, #86 병합) | 04, 05 (06 결과가 없으면 인프라 항목을 "미수집"으로 표시) | AV-006, AV-017 | #59 | ☑ |
| [08A 에이전트 실행 기반과 Skill 형식](../../tickets/analysis-verification/08a-agent-runtime.md) | DONE (PR #68 병합) | 01 | AV-007, AV-020 | #67 | ☑ |
| [08 카탈로그·Skill v1과 매핑](../../tickets/analysis-verification/08-mapping.md) | DONE (PR #73 병합) | 07, 08A, D-01(실제 LLM 연결 시) | AV-007, AV-008, AV-020 | #72 | ☑ |
| [08B 필수 기능 확정과 손실 승인 게이트](../../tickets/analysis-verification/08b-review-gate.md) | DONE (PR #80 병합) | 02, 08 | AV-022, AV-008 | #78 | ☑ |
| [09 소스 묶음 생성과 S3 업로드](../../tickets/analysis-verification/09-source-archive.md) | DONE (PR #84 병합) | 03 | AV-009, AV-020 | #83 | ☑ |
| [10 Build 모듈 연동과 실패 분류](../../tickets/analysis-verification/10-build-integration.md) | DONE (PR #88 병합) | 09, Provisioning B-01(DONE) | AV-010 | #87 | ☑ |
| [11 Sandbox 구성·적용·정리](../../tickets/analysis-verification/11-sandbox-apply.md) | IN_PROGRESS (구현 완료, 리뷰 대기) | 02B, 07, Provisioning 공통 렌더 v2(#24, DONE) | AV-011, AV-016, AV-020 | #93 | ☐ |
| [11B Sandbox 제자리 갱신과 테스트 데이터 초기화](../../tickets/analysis-verification/11b-sandbox-update.md) | IN_PROGRESS (구현 완료, 리뷰 대기) | 11 | AV-011, AV-015 | #95 | ☐ |
| [12 테스트 묶음·Tester·`run_suite`](../../tickets/analysis-verification/12-test-suite.md) | DRAFT | 07, 08A, 11 | AV-012, AV-020 | 미생성 | ☐ |
| [13 통신 규칙·판정·실행 기록](../../tickets/analysis-verification/13-judge-telemetry.md) | DRAFT | 08, 11, 12 | AV-013, AV-014 | 미생성 | ☐ |
| [14 실패 분류·Fixer 루프·최종 발행·Draft PR](../../tickets/analysis-verification/14-fix-loop.md) | DRAFT | 08, 08B, 10, 11B, 13 | AV-015, AV-017, AV-019, AV-020 | 미생성 | ☐ |
| [15 Planning 인계(입력 v2)](../../tickets/analysis-verification/15-planning-handoff.md) | DRAFT | 14, D-02(저장 경계) | AV-018 | 미생성 | ☐ |
| [16 데모 앱 통합 검증](../../tickets/analysis-verification/16-demo-e2e.md) | DRAFT | 06, 15, 데모 앱 준비 | AV-001~AV-022, spec §7 | 미생성 | ☐ |
| [17 코드 색인과 에이전트 조회 도구](../../tickets/analysis-verification/17-code-index.md) | DRAFT(후순위) | 05, 08 | AV-003, AV-007, AV-020 | 미생성 | ☐ |
| [18 공개 GitHub 저장소 선택 결과의 분석 입력 연결](../../tickets/analysis-verification/18-public-github-source.md) | TODO (담당자 계약 조율 전) | 02, 03, #56 병합 | AV-001, AV-002, AV-020 | #60 | ☐ |
| [19 분석 Activity 통합](../../tickets/analysis-verification/19-analysis-integration.md) | IN_PROGRESS (구현 완료, 리뷰 대기) | 03~08B, 06 | AV-001~AV-008, AV-017, AV-020, AV-021 | #91 | ☐ |

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
07 + 08A + 11 → 12
08 + 11 + 12 → 13
08 + 08B + 10 + 11B + 13 → 14 → 15 → 16
06 → 16
05 + 08 → 17 (후순위)
```

정확한 선행 조건은 위 표와 각 티켓을 따른다. D-05는 Sandbox 전용 Queue를 추가하기로 결정했다. 외부 의존: 06은 AWS 테스트 계정, 08은 D-01(실제 LLM 연결 시), 10은 B-01(완료)과 D-12, 11은 공통 렌더 v2(완료)와 D-11, 15는 D-02, 16은 데모 앱·Sandbox 기반(D-08).

**추천 순서:** 01 → Q-01 → 02 → 02B → 03 → 04 → 05 → 07 → 08A → 08 → 08B → 09 → 10 → 11 → 11B → 12 → 13 → 14 → (06, 15) → 16 → 17. 외부 의존이 없는 것부터 이어 첫 사이클 뼈대(스캔 → 빌드 → Sandbox 루프)를 먼저 완성한다. 17은 첫 사이클이 끝난 뒤 착수한다. 18은 02·03 구현을 재사용하되 #56 병합과 분석 담당자 계약 확인 후 별도로 진행한다.

## 결정 대기

| ID | 내용 | 영향 티켓 | 담당 |
| --- | --- | --- | --- |
| D-01 | 공급자는 Claude, 연결은 Anthropic SDK(Messages API 도구 호출)로 결정. 모델·코드 전송 범위(근거 주변 조각만 전송)·키 관리·비용 한도는 ADR-004 작성 예정 | 08, 12, 14, 17 | 승철·팀 |
| D-02 | 결정됨(승철) — 첫 사이클의 `*_ref`는 해시 이름 로컬 불변 저장소(`LocalArtifactStore` 관례, S3는 후속). 분석·Build 결과의 저장·정규화는 분석 쪽이 맡음 | 10, 15, 19 | 승철 |
| D-04 | Build 실패 상세(`BUILD_FAILED`/`PUSH_FAILED` 분리, 정제된 출력 일부) — 급하지 않음 | 10, 14 | 민서 |
| D-05 | 결정됨 — `sandbox` Queue 분리 | Q-01, 02B | 승철(민서 공유) |
| D-06 | 소스·테스트 묶음 버킷 소유·이름·정리 규칙, Sandbox 이미지 보존 기간 | 09, 10, 12 | 민서·승철 |
| D-07 | 결정됨 — 첫 사이클 지원 언어는 Go(데모 앱 `demo-app`이 Go) | 05, 16, 17 | 승철 |
| D-08 | 결정됨(승철) — Sandbox 기반은 이 Mac의 로컬 k3d `cmp-sandbox`(Colima, 전용 kubeconfig)로 준비. 설정 방법은 11 티켓의 환경 준비 참조 | 11, 16 | 승철 |
| D-09 | AI 테스트 1번 조건용 원본 쪽 테스트 자원 | 12 | 승철·소울 |
| D-10 | Production 테스트 묶음 실행 시점(데이터 이전 후·트래픽 전환 전)·데이터 정리 | 15 | 민서·소울·상헌 |
| D-11 | DB 이미지를 고르는 "대상 구성 단계"의 소유자, Sandbox·Production 공유 방식(요구사항엔 이미지 없음, #22) | 11 | 민서 |
| D-12 | Build 호출 Worker(권한 배치)·전체 시간 상한(`resource_id`·`source_version_id`는 구현됨) | 10 | 민서 |
| D-13 | DB 내부 상태 사전 점검 담당(기본 키·시퀀스·복제 설정) | 07 | 소울 |
| D-14 | PostgreSQL 확장 지원 시점과 `CREATE EXTENSION` 실행 주체(현재 확장 요구는 Planning이 거절) | 07 | 민서 |
| D-15 | 외부 진입(인터넷 → 앱)을 `network_intent`로 표현하는 방법(그 전까지 `AnalysisReport`에 기록) | 01, 06, 13 | 민서 |
| D-16 | 결정됨, V2·렌더러 구현 완료(#79, #81), 07 바인딩 반영 — DB 연결을 단일 URL 변수로 받는 앱은 `env_bindings`에 `connection_url_secret_ref` 출력으로 바인딩. Sandbox와 운영이 각각 URL을 만들어 Secret에 저장하고 앱에는 Secret 이름·키만 주입. 형식(URL 인코딩, Secret 키 이름)은 민서님과 확정 | 07, 11 | 민서(V2·공통 렌더러·Secret 주입)·승철(07 바인딩, 11 검증) |

## 변경 기록

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
- 2026-11-04(팀 확인): 민서님이 분석·Sandbox 자식 Workflow 분리 방향에 이견이 없다고 확인. 새 종료 상태(`needs_review`, `stopped_render`)는 기존 완료·취소에 끼워 맞추지 않고 검토 대기·실패·완료를 구분해 API(SSE 종료 조건)와 함께 정리하기로 함(API는 민서님 담당). 자식에 직접 Signal을 보내는 설계와 권한·상태 이중 확인도 동의. 신규 Worker 배포 시 동작 이상 여부는 별도 확인 항목.
- 2026-11-04(리뷰 반영): spec AV-012에 `run_suite`의 `include`, AV-015에 INFRA와 횟수 규칙 반영(AV-013 커버리지는 원문 유지). ADR-004는 작성 예정으로 표시.
- 2026-11-04: D-05 결정: `sandbox` Task Queue를 분리한다. Q-01에서 Worker 등록, 02·02B에서 범위를 한정한 Workflow 연결을 수행한다.
- 2026-11-04: 분석을 고정 ID `analysis-<migration_id>`의 자식 Workflow로 분리하고, 검토 Signal은 상태 Query 후 자식에 직접 보낸다. Q-01은 Temporal 테스트 환경을 기본 기동 검증으로 사용한다.
- 2026-11-04(목적 점검): 원래 목적(필수 기능을 잃지 않고 옮김) 대비 빠진 단계를 보완. AV-022(필수 기능 확정·손실 승인)와 08B 게이트, 에이전트 실행 기반·Skill 형식(08A, plan §2.11) 추가. 14에 변경 행 재승인과 매핑 행 `SANDBOX_VERIFIED` 갱신 추가.
- 2026-11-04(v2 동기화): main에 병합된 v2 구현(#22 Planning 입력 v2·`RequirementsV2`, #24 공통 렌더 v2, B-01 `resource_id`·`source_version_id`) 기준으로 spec·plan·티켓을 맞춤. 요구사항은 `RequirementsV2` 재사용, DB 이미지는 대상 구성 단계(D-11), 확장 요구는 차단 보고(D-14), 외부 진입 표현은 D-15로 분리. D-07을 '첫 사이클 지원 언어(승철 결정)'로 변경.
- 2026-11-04: 민서님 계약 협의안 v2 반영(리소스 목록, capabilities, 변수별 env_bindings, validated_workloads, 배포 후 테스트 시점). 호출부 결정 방식에 따라 Sandbox 구체 값·Secret·NetworkPolicy를 Sandbox 워커 책임으로 명시. 리뷰 피드백으로 02→02·02B, 11→11·11B 분할, Q-01 신설, 01 완료 조건 축소, AnalysisReport 이름 변경. D-11~D-14 추가.

## 후속(첫 사이클 이후, 티켓 미작성)

- S3: `storage-s3` 카탈로그·Skill, 엔드포인트·인증 코드 수정 → 같은 루프. presigned URL 사례. 첫 사이클에서는 `KEEP`.
- Dockerfile 없는 앱의 Fixer 생성 고도화, 운영 이미지 재사용 대체 경로.
- 실행 커버리지 계측 이미지, 내부 Ingress 경로 검증(차트 지원 후).
- 대상 나란히 비교(GCP vs 온프레미스), 놓친 의존성 자동 반영, 매핑 정확도 측정.
