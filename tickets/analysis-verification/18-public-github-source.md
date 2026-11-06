# Ticket 18 — 공개 GitHub 저장소 선택 결과의 분석 입력 연결

## 상태
IN_PROGRESS — Issue #60. 시작 요청이 공개 GitHub 소스(`GITHUB_PUBLIC`)를 분석 입력으로 연결하는 부분은 Provisioning 44(PR #206)로 구현됨(`start_input`이 저장소 URL과 커밋을 `AnalysisInput`에 넣음). 남음: API가 저장소 공개 여부·커밋 존재를 GitHub로 재확인하는 부분(코드에서 찾지 못함). 비공개 저장소와 토큰은 [티켓 23](23-source-access.md)이 다룬다.

## 목적
웹에서 선택한 공개 GitHub 저장소와 커밋을 기존 소스 확보 경로에 전달한다. 웹은 소스 파일이나 GitHub 로그인 토큰을 플랫폼에 보내지 않고, 분석 워커가 공개 저장소를 직접 가져온다.

## 관련 요구사항
AV-001, AV-002, AV-020

## 선행 작업
Blocked by: 02, 03, 진행 중인 Provisioning 14(#56) 병합 및 분석 담당자와 입력 계약 확인.

## 현재 구현과 재사용 지점
- `MigrationRequest`는 소스 입력이 없는 scaffold이며 `POST /migrations`는 `project_id`만 부모 Workflow에 전달한다.
- `AnalysisWorkflow.run(migration_id)`의 `fetch_source` Activity는 아직 scaffold다.
- `cmp_scanners.source.fetch_source(repository, requested_commit, migration_id, workspace_root, token=None)`는 구현되어 있다. 공개 저장소는 `token=None`으로 사용한다.
- `AnalysisRequest.source_access_ref`는 필수다. 공개 소스의 무인증 의미와 요청 SHA 표현은 담당자와 맞춰 최소한으로 정한다. 가짜 참조를 만들지 않는다.

## 웹과 합의할 입력
`source: {kind: "GITHUB_PUBLIC", repository_id: number, full_name: "owner/repo", commit_sha: "40자리 SHA"}`. 기존 프로젝트·대상 입력과 합치는 방식은 현재 API 계약을 확인해 정한다.

## 작업 범위
- API가 공개 저장소·ID·커밋을 재확인하고 비공개·불일치·임의 URL 입력을 거절한다.
- 검증된 저장소와 커밋을 현재 API → Migration/Analysis Workflow → `fetch_source` Activity 경계에 맞춰 전달한다. 새 DB 테이블이나 특정 Workflow 인자 변경 방식을 미리 확정하지 않는다.
- 기존 소스 확보 함수와 운영 이미지 커밋 우선·폴백·`commit_verified` 정책을 재사용한다. 로컬 작업 공간 경로를 다른 Worker가 읽을 수 있는 공유 참조로 취급하지 않는다.
- 실제 변경에 맞춰 분석 SDD·HTTP 계약·티켓 인덱스를 구현 PR에 갱신한다.

## 제외
- 웹 Auth.js·로그인·저장소 목록/선택 UI, 비공개 저장소·GitHub App·토큰 보관.
- 코드·매니페스트 스캐너 자체 구현(04/05), S3 소스 아카이브(09), Fixer·Draft PR(14), Provisioning 14(#56) 복구 로직.
- 이 티켓만으로 전체 분석·마이그레이션 완료를 주장하지 않는다.

## 수용 기준
- [ ] 공개 저장소 선택의 ID·이름·요청 SHA를 검증해 기존 `fetch_source(..., token=None)`에 전달한다.
- [ ] 잘못된 저장소 ID, 비공개 저장소, 없는 커밋, 허용되지 않은 URL은 소스 확보 전에 거절된다.
- [ ] GitHub 토큰·소스 코드 바이트·Secret이 HTTP 응답·로그·Temporal 이력에 실리지 않는다.
- [ ] 운영 커밋 확인 실패 시 `commit_verified=false` 의미가 유지되고, 기존 source 없는 scaffold 요청·Workflow replay가 유지된다.
- [ ] API와 소스 확보 경계를 가짜 GitHub 응답·로컬 Git fixture로 검증한다. 실제 스캐너 결과나 S3 업로드를 이 티켓의 완료 증거로 삼지 않는다.

## 테스트 전략
- 제안 Test Seam: API 검증 경계의 가짜 GitHub 응답과 `fetch_source` 공개 함수의 로컬 Git fixture. 잡는 것: 잘못된 입력 차단·요청 커밋 고정·토큰 없는 전달. 못 잡는 것: 실제 GitHub 장애·대용량 clone 성능.
- 구현 시 Python 변경 후 `make check`, HTTP 변경 후 `make openapi`, Temporal 변경 시 가능한 환경에서 replay·smoke 결과를 기록한다.

## 협업
- 분석 Workflow·Activity와 `cmp_scanners.source`는 분석 담당자의 작업 범위다. 구현 전 해당 담당자와 입력 계약 및 수정 범위를 합의한다.
- #56 진행 브랜치를 건드리지 않고 병합 후 최신 `main`에서 공유 파일 충돌을 확인한다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [GitHub Issue #60](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/60)
