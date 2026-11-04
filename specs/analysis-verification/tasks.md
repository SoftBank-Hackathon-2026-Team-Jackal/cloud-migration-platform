# Tasks: 분석·매핑·Sandbox 검증

이 파일은 분석·검증 티켓의 **인덱스**다. 각 티켓의 수용 기준·Test Seam·결정 대기는 연결된 파일에 있다. 진행 방식은 [개발 워크플로](../../docs/engineering/development-workflow.md)를 이 기능에도 준용한다: 상태는 `DRAFT → TODO → IN_PROGRESS → DONE`, 막히면 `BLOCKED`. 사용자 검토 후 `TODO`로 바꾸고, 구현 전에 GitHub Issue를 만들거나 연결해 번호를 아래에 적는다. 한 번에 한 티켓씩 진행한다.

| ID·티켓 | 상태 | Blocked by | 관련 요구사항 | GitHub Issue | 완료 |
| --- | --- | --- | --- | --- | --- |
| [01 분석·검증 계약 모델](../../tickets/analysis-verification/01-contracts.md) | DONE (PR #31 병합) | 없음 | AV-001, AV-017, AV-018, AV-020, AV-022 | #29 | ☑ |
| [Q-01 `sandbox` Task Queue·Worker 등록](../../tickets/analysis-verification/q-01-sandbox-queue.md) | DONE (PR #33 병합) | 없음 (D-05 결정) | AV-021 | #32 | ☑ |
| [02 분석 Activity 골격](../../tickets/analysis-verification/02-temporal-skeleton.md) | DONE (PR #46 병합) | 01 | AV-001, AV-021 | #44 | ☑ |
| [02B Sandbox 자식 Workflow 골격](../../tickets/analysis-verification/02b-sandbox-workflow.md) | DONE (PR #46 병합) | 02, Q-01 | AV-014, AV-015, AV-016, AV-021 | #45 | ☑ |
| [03 소스 확보·운영 커밋 고정·마스킹](../../tickets/analysis-verification/03-source-fetch.md) | DRAFT | 01 | AV-002, AV-020 | 미생성 | ☐ |
| [04 매니페스트·클러스터 리소스 스캔](../../tickets/analysis-verification/04-manifest-scan.md) | DRAFT | 01 | AV-004 | 미생성 | ☐ |
| [05 코드 스캔과 `.env` 대조](../../tickets/analysis-verification/05-code-scan.md) | DRAFT | 03, D-07(언어별 어댑터만) | AV-003 | 미생성 | ☐ |
| [06 AWS 인프라·네트워크 스캔과 통신 의도](../../tickets/analysis-verification/06-aws-infra-scan.md) | DRAFT | 01, AWS 테스트 계정(외부) | AV-005, AV-020 | 미생성 | ☐ |
| [07 요구사항·사용 프로필·사전 점검 조립](../../tickets/analysis-verification/07-profile-precheck.md) | DRAFT | 04, 05 (06 결과가 없으면 인프라 항목을 "미수집"으로 표시) | AV-006, AV-017 | 미생성 | ☐ |
| [08A 에이전트 실행 기반과 Skill 형식](../../tickets/analysis-verification/08a-agent-runtime.md) | DRAFT | 01 | AV-007, AV-020 | 미생성 | ☐ |
| [08 카탈로그·Skill v1과 매핑](../../tickets/analysis-verification/08-mapping.md) | DRAFT | 07, 08A, D-01(실제 LLM 연결 시) | AV-007, AV-008, AV-020 | 미생성 | ☐ |
| [08B 필수 기능 확정과 손실 승인 게이트](../../tickets/analysis-verification/08b-review-gate.md) | DRAFT | 02, 08 | AV-022, AV-008 | 미생성 | ☐ |
| [09 소스 묶음 생성과 S3 업로드](../../tickets/analysis-verification/09-source-archive.md) | DRAFT | 03 | AV-009, AV-020 | 미생성 | ☐ |
| [10 Build 모듈 연동과 실패 분류](../../tickets/analysis-verification/10-build-integration.md) | DRAFT | 09, Provisioning B-01(DONE) | AV-010 | 미생성 | ☐ |
| [11 Sandbox 구성·적용·정리](../../tickets/analysis-verification/11-sandbox-apply.md) | DRAFT | 02B, 07, Provisioning 공통 렌더 v2(#24, DONE) | AV-011, AV-016, AV-020 | 미생성 | ☐ |
| [11B Sandbox 제자리 갱신과 테스트 데이터 초기화](../../tickets/analysis-verification/11b-sandbox-update.md) | DRAFT | 11 | AV-011, AV-015 | 미생성 | ☐ |
| [12 테스트 묶음·Tester·`run_suite`](../../tickets/analysis-verification/12-test-suite.md) | DRAFT | 07, 08A, 11 | AV-012, AV-020 | 미생성 | ☐ |
| [13 통신 규칙·판정·실행 기록](../../tickets/analysis-verification/13-judge-telemetry.md) | DRAFT | 08, 11, 12 | AV-013, AV-014 | 미생성 | ☐ |
| [14 실패 분류·Fixer 루프·최종 발행·Draft PR](../../tickets/analysis-verification/14-fix-loop.md) | DRAFT | 08, 08B, 10, 11B, 13 | AV-015, AV-017, AV-019, AV-020 | 미생성 | ☐ |
| [15 Planning 인계(입력 v2)](../../tickets/analysis-verification/15-planning-handoff.md) | DRAFT | 14, D-02(저장 경계) | AV-018 | 미생성 | ☐ |
| [16 데모 앱 통합 검증](../../tickets/analysis-verification/16-demo-e2e.md) | DRAFT | 06, 15, 데모 앱 준비 | AV-001~AV-022, spec §7 | 미생성 | ☐ |
| [17 코드 색인과 에이전트 조회 도구](../../tickets/analysis-verification/17-code-index.md) | DRAFT(후순위) | 05, 08 | AV-003, AV-007, AV-020 | 미생성 | ☐ |

## 선행 관계

```text
01 → 02 ─┐
Q-01 ────┴→ 02B ─┐
01 → 03 → 05 ─┐  │
01 → 04 ──────┴→ 07 ─┬→ 08 → 08B (02 필요)
01 → 08A ────────────┘ │ (08은 08A 필요)
              06 ┄┄→ 07 (선택 입력)
03 → 09 → 10           │
02B + 07 → 11 → 11B    │
07 + 08A + 11 → 12
08 + 11 + 12 → 13
08 + 08B + 10 + 11B + 13 → 14 → 15 → 16
06 → 16
05 + 08 → 17 (후순위)
```

정확한 선행 조건은 위 표와 각 티켓을 따른다. D-05는 Sandbox 전용 Queue를 추가하기로 결정했다. 외부 의존: 06은 AWS 테스트 계정, 08은 D-01(실제 LLM 연결 시), 10은 B-01(완료)과 D-12, 11은 공통 렌더 v2(완료)와 D-11, 15는 D-02, 16은 데모 앱·Sandbox 기반(D-08).

**추천 순서:** 01 → Q-01 → 02 → 02B → 03 → 04 → 05 → 07 → 08A → 08 → 08B → 09 → 10 → 11 → 11B → 12 → 13 → 14 → (06, 15) → 16 → 17. 외부 의존이 없는 것부터 이어 첫 사이클 뼈대(스캔 → 빌드 → Sandbox 루프)를 먼저 완성한다. 17은 첫 사이클이 끝난 뒤 착수한다.

## 결정 대기

| ID | 내용 | 영향 티켓 | 담당 |
| --- | --- | --- | --- |
| D-01 | LLM 공급자·모델·코드 전송 범위(ADR-004 작성 예정, 근거 주변 조각만 전송) | 08, 12, 14, 17 | 승철·팀 |
| D-02 | `*_ref` 저장 위치, Build 결과 저장·정규화 Activity 담당(Planning 입력 v2 형식은 #22로 확정) | 10, 15 | 민서·승철 |
| D-04 | Build 실패 상세(`BUILD_FAILED`/`PUSH_FAILED` 분리, 정제된 출력 일부) — 급하지 않음 | 10, 14 | 민서 |
| D-05 | 결정됨 — `sandbox` Queue 분리 | Q-01, 02B | 승철(민서 공유) |
| D-06 | 소스·테스트 묶음 버킷 소유·이름·정리 규칙, Sandbox 이미지 보존 기간 | 09, 10, 12 | 민서·승철 |
| D-07 | 첫 사이클 지원 언어(데모 앱은 그 언어에 맞춤) | 05, 16, 17 | 승철 |
| D-08 | Sandbox 기반 클러스터 준비 주체·시점 | 11, 16 | 민서·승철 |
| D-09 | AI 테스트 1번 조건용 원본 쪽 테스트 자원 | 12 | 승철·소울 |
| D-10 | Production 테스트 묶음 실행 시점(데이터 이전 후·트래픽 전환 전)·데이터 정리 | 15 | 민서·소울·상헌 |
| D-11 | DB 이미지를 고르는 "대상 구성 단계"의 소유자, Sandbox·Production 공유 방식(요구사항엔 이미지 없음, #22) | 11 | 민서 |
| D-12 | Build 호출 Worker(권한 배치)·전체 시간 상한(`resource_id`·`source_version_id`는 구현됨) | 10 | 민서 |
| D-13 | DB 내부 상태 사전 점검 담당(기본 키·시퀀스·복제 설정) | 07 | 소울 |
| D-14 | PostgreSQL 확장 지원 시점과 `CREATE EXTENSION` 실행 주체(현재 확장 요구는 Planning이 거절) | 07 | 민서 |
| D-15 | 외부 진입(인터넷 → 앱)을 `network_intent`로 표현하는 방법(그 전까지 `AnalysisReport`에 기록) | 01, 06, 13 | 민서 |

## 변경 기록

- 2026-11-05: 02·02B를 PR #46으로 병합(이슈 #44, #45 종료). 14·15의 부모 Workflow 변경 범위를 plan에 기록하고 14에 종료 상태 정리 수용 기준을 추가.
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
