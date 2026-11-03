# Tasks: 분석·매핑·Sandbox 검증

이 파일은 분석·검증 티켓의 **인덱스**다. 각 티켓의 수용 기준·Test Seam·결정 대기는 연결된 파일에 있다. 진행 방식은 [개발 워크플로](../../docs/engineering/development-workflow.md)를 이 기능에도 준용한다: 상태는 `DRAFT → TODO → IN_PROGRESS → DONE`, 막히면 `BLOCKED`. 사용자 검토 후 `TODO`로 바꾸고, 구현 전에 GitHub Issue를 만들거나 연결해 번호를 아래에 적는다. 한 번에 한 티켓씩 진행한다.

| ID·티켓 | 상태 | Blocked by | 관련 요구사항 | GitHub Issue | 완료 |
| --- | --- | --- | --- | --- | --- |
| [01 분석·검증 계약 v1](../../tickets/analysis-verification/01-contracts.md) | DRAFT | 없음 | AV-001, AV-017, AV-018, AV-020 | 미생성 | ☐ |
| [02 Temporal 골격과 Sandbox 자식 Workflow](../../tickets/analysis-verification/02-temporal-skeleton.md) | DRAFT | 01 | AV-001, AV-015, AV-016, AV-021 | 미생성 | ☐ |
| [03 소스 확보·운영 커밋 고정·마스킹](../../tickets/analysis-verification/03-source-fetch.md) | DRAFT | 01 | AV-002, AV-020 | 미생성 | ☐ |
| [04 매니페스트·클러스터 리소스 스캔](../../tickets/analysis-verification/04-manifest-scan.md) | DRAFT | 01 | AV-004 | 미생성 | ☐ |
| [05 코드 스캔과 `.env` 대조](../../tickets/analysis-verification/05-code-scan.md) | DRAFT | 03 | AV-003 | 미생성 | ☐ |
| [06 AWS 인프라·네트워크 스캔과 네트워크 의도](../../tickets/analysis-verification/06-aws-infra-scan.md) | DRAFT | 01, AWS 테스트 계정(외부) | AV-005, AV-020 | 미생성 | ☐ |
| [07 사용 프로필·사전 점검·분석 결과 조립](../../tickets/analysis-verification/07-profile-precheck.md) | DRAFT | 04, 05 (06 결과가 없으면 인프라 항목을 "미수집"으로 표시) | AV-006, AV-017 | 미생성 | ☐ |
| [08 카탈로그·Skill v1과 매핑](../../tickets/analysis-verification/08-mapping.md) | DRAFT | 07, D-01(실제 LLM 연결 시) | AV-007, AV-008, AV-020 | 미생성 | ☐ |
| [09 소스 묶음 생성과 S3 업로드](../../tickets/analysis-verification/09-source-archive.md) | DRAFT | 03 | AV-009, AV-020 | 미생성 | ☐ |
| [10 Build 모듈 연동과 실패 분류](../../tickets/analysis-verification/10-build-integration.md) | DRAFT | 09, Provisioning B-01(DONE) | AV-010 | 미생성 | ☐ |
| [11 Sandbox 적용·갱신·초기화·정리](../../tickets/analysis-verification/11-sandbox-apply.md) | DRAFT | 02, Provisioning 02·03(DONE) | AV-011, AV-016, AV-020 | 미생성 | ☐ |
| [12 테스트 묶음과 Tester](../../tickets/analysis-verification/12-test-suite.md) | DRAFT | 07, 11 | AV-012, AV-020 | 미생성 | ☐ |
| [13 판정과 실행 기록](../../tickets/analysis-verification/13-judge-telemetry.md) | DRAFT | 11, 12 | AV-013, AV-014 | 미생성 | ☐ |
| [14 실패 분류·Fixer 루프·Draft PR](../../tickets/analysis-verification/14-fix-loop.md) | DRAFT | 08, 10, 13 | AV-015, AV-019, AV-020 | 미생성 | ☐ |
| [15 Planning 인계(SandboxResult v2)](../../tickets/analysis-verification/15-planning-handoff.md) | DRAFT | 14, Provisioning Planning v2 합의 | AV-018 | 미생성 | ☐ |
| [16 데모 앱 통합 검증](../../tickets/analysis-verification/16-demo-e2e.md) | DRAFT | 06, 15, 데모 앱 준비 | AV-001~AV-021, spec §7 | 미생성 | ☐ |

## 선행 관계

```text
01 ─┬→ 02 ───────────────→ 11
    ├→ 03 ─┬→ 05 ─┐
    │      └→ 09 → 10
    ├→ 04 ────────┴→ 07 → 08
    └→ 06 (07에 선택 입력)
07 + 11 → 12 → 13
08 + 10 + 13 → 14 → 15 → 16
06 → 16
```

정확한 선행 조건은 위 표와 각 티켓을 따른다. 외부 의존: 06은 AWS 테스트 계정, 08은 D-01(실제 LLM 연결 시), 10은 B-01(완료), 11은 Provisioning 02·03(완료), 15는 Planning v2 합의, 16은 데모 앱.

**추천 순서:** 01 → 02 → 03 → 04 → 05 → 07 → 09 → 10 → 11 → 08 → 12 → 13 → 14 → (06, 15) → 16. 외부 의존이 없는 것부터 이어 첫 사이클 뼈대(스캔 → 빌드 → Sandbox 루프)를 먼저 완성한다.

## 결정 대기

| ID | 내용 | 영향 티켓 | 담당 |
| --- | --- | --- | --- |
| D-01 | LLM 공급자·모델·코드 전송 범위(ADR-004로 기록) | 08, 12, 14 | 승철·팀 |
| D-02 | Planning `SandboxResult`/`BuildResult` v2 필드, `*_ref` 저장 위치 | 01, 15 | 민서·승철 |
| D-03 | 통합 Sandbox 렌더 함수, 앱 Chart의 내부 Ingress·NetworkPolicy | 11, 13 | 민서 |
| D-04 | Build 실패 상세(`BUILD_FAILED`/`PUSH_FAILED` 분리, 정제된 출력 일부) | 10, 14 | 민서 |
| D-05 | `sandbox` Task Queue 추가 여부 | 02 | 승철(민서 공유) |
| D-06 | 소스 묶음 버킷 소유·이름·정리 규칙, Sandbox 이미지 보존 기간 | 09, 10 | 민서·승철 |
| D-07 | 데모 앱 언어·API·DB 형태(RDS vs Pod)·Dockerfile/매니페스트 위치 | 05, 07, 16 | 소울 |
| D-08 | Sandbox 기반 클러스터 준비 주체·시점 | 11, 16 | 민서·승철 |
| D-09 | AI 테스트 1번 조건용 원본 쪽 테스트 자원 | 12 | 승철·소울 |
| D-10 | Production에서 테스트 묶음 재실행 시 데이터·계정·정리 정책 | 15 | 팀 |

## 후속(첫 사이클 이후, 티켓 미작성)

- S3: `storage-s3` 카탈로그·Skill, 엔드포인트·인증 코드 수정 → 같은 루프. presigned URL 사례.
- Dockerfile 없는 앱의 Fixer 생성 고도화, 운영 이미지 재사용 대체 경로.
- 실행 커버리지 계측 이미지.
- 대상 나란히 비교(GCP vs 온프레미스), 놓친 의존성 자동 반영, 매핑 정확도 측정.
