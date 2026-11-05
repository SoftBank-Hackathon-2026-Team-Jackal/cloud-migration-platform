# Ticket 25 — 빌드 연결(소스 묶음 S3 업로드 → Build 모듈 → 빌드 증거)

## 상태
IN_PROGRESS — Issue #140 (사용자 검토 완료, 2026-11-05)

## 목적
지금 Sandbox 루프의 앱 이미지는 사람이 적어 준 Digest(`PrebuiltImages`)다. 설정이 있으면 실제 경로 — 소스 묶음을 S3에 올리고, 민서님의 Build 모듈로 이미지를 빌드하고, 결과를 `BuildAttempt` 증거로 남기는 경로 — 를 쓰게 한다. 테스트 묶음도 S3에 올려 Planning이 요구하는 `S3ArchiveRef`를 만든다. 15번(Planning 인계)이 이 증거를 쓴다.

## 관련 요구사항
AV-009, AV-010, AV-018

## 선행 작업
Blocked by: 09, 10, 22 (모두 병합됨). 실제 AWS 실행은 D-06(버킷 소유·이름·정리)과 D-12(Build 호출 Worker·시간 상한) 결정과 AWS 계정 사용 승인이 있어야 한다. 이 티켓의 구현·테스트는 가짜 저장소·가짜 Build로 한다.

## 작업 범위
- 포함
  - 설정 `BUILD_SOURCE_BUCKET`, `BUILD_ECR_REPOSITORY`, `BUILD_LOG_BUCKET`, `BUILD_PLATFORM`, `BUILD_AWS_PROFILE`(이름만)과 전부 있을 때만 실제 빌드를 켜는 조립. 하나라도 없으면 지금처럼 `PrebuiltImages`로 동작(빌드 증거는 "없음"으로 표시)
  - 루프의 `build` 호출자: `package_source`(09)로 소스 묶음을 만들어 올리고(`AwsCliArchiveStore`), `request_build`(10)로 빌드, 결과(`BuildResult`)에서 `Image`(URI@Digest)와 `BuildAttempt`를 만든다. 같은 소스·플랫폼은 재사용(10의 재사용 규칙)
  - 빌드 증거 저장: `BuildAttempt` 목록을 분석 저장소에 해시 이름 문서로 저장하고 `sbx_build` 결과와 Sandbox Workflow 결과(`builds_ref`)에 참조로 싣는다
  - 테스트 묶음 업로드: 고정된 묶음(`pin_suite`)을 결정적 tar.gz로 감싸 `s3://<bucket>/tests/<sha256>.tar.gz`에 올리고 `S3ArchiveRef`를 만든다(`test_suite_ref`). 묶음 자체의 해시(`run_suite`용)는 그대로
  - 사전 점검(`preflight`)에 빌드 설정 누락·계정 확인 안내 추가(계정 호출은 하지 않음)
- 제외: 실제 AWS 호출(승인 후 별도 확인), Build 모듈 자체 변경(민서님), ECR 저장소·버킷 생성(Terraform·민서님), 수명주기·정리 정책(D-06)

## 수용 기준
- [x] 빌드 설정이 모두 있을 때만 실제 경로가 켜지고, 일부만 있으면 시작 시 이유 코드로 거절된다. 없으면 이전 동작과 같다.
- [x] 소스는 같은 내용이면 같은 해시 키로 한 번만 올라가고, 비밀 파일(`.env` 등)은 묶음에 들어가지 않는다(09 규칙 유지).
- [x] 빌드 성공 시 `Image`는 Digest로 고정되고, `BuildAttempt`에 소스 `S3ArchiveRef`·Digest·플랫폼·시도 ID가 남는다. 실패는 분류 코드(10)와 함께 Sandbox 루프의 BUILD/INFRA 처리로 이어진다.
- [x] 코드 수정 후 재빌드는 새 소스 해시로 올리고 새 증거를 남기며, 이전 증거는 지우지 않는다.
- [x] 테스트 묶음 `S3ArchiveRef`의 URI 마지막 이름이 그 아카이브 해시와 같고, 같은 묶음은 항상 같은 해시가 된다.
- [x] 자격 증명·토큰은 입력·이력·로그·결과에 나타나지 않는다(이름 있는 프로필만 쓴다).

## 테스트 전략
- 제안 Test Seam: 가짜 `ArchiveStore`와 가짜 Build 모듈(`builder_factory`)로 호출자·증거·재사용·실패 분류, 설정 조립(순수), 결정적 tar.gz(순수), 기존 루프 테스트의 `build` 자리에 새 호출자를 끼워 회귀 확인. 잡는 것: 증거 정합성, 재사용·재빌드, 설정 누락. 못 잡는 것: 실제 S3·ECR·Docker(승인 후 1회 수동 확인).
- 실행: `uv run pytest tests/sandbox tests/analysis -q`, `uv run ruff check .`

## 문서
tasks.md 표·변경 기록, runbook(빌드 설정, `PrebuiltImages`와의 관계), spec AV-009·AV-010 비고를 갱신하고, 민서님께는 Build 모듈을 호출하는 방식(변경 없음)과 필요한 버킷·ECR·권한 목록을 통보한다.
