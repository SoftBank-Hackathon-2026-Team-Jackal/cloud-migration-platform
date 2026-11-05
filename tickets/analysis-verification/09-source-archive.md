# Ticket 09 — 소스 묶음 생성과 S3 업로드

## 상태
DONE — PR #84 병합, Issue #83

## 목적
Build 모듈 입력인 SHA-256 이름의 재현 가능한 소스 아카이브를 만들고 S3에 올린다.

## 관련 요구사항
AV-009, AV-020

## 선행 작업
Blocked by: 03

## 작업 범위
- 포함: `package_source`: 파일 목록(`.dockerignore`, `.git`·`.env`·인증 파일 제외, 일반 파일만), Dockerfile 루트 배치, 재현 가능한 tar.gz, 같은 키 존재 시 업로드 생략, 리소스 ID별 `source_refs`
- 제외: Dockerfile 생성(14의 Fixer)

## 수용 기준
- [x] 같은 입력을 두 번 묶으면 같은 SHA-256이 나온다.
- [x] `.git`, `.env`, 심볼릭 링크, 경로 이탈 항목이 들어가지 않는다.
- [x] 만든 아카이브가 B-01 아카이브 검사(경로·링크·`.env`·크기)를 통과한다.
- [x] 마스킹본이 아니라 원본으로 묶인다.
- [x] 같은 해시가 이미 있으면 업로드하지 않는다.

## 테스트 전략
- 합의된 Test Seam: `package_source(workspace)`가 돌려주는 바이트·해시·URI. 잡는 것: 재현성·제외 규칙. 못 잡는 것: 실제 S3 권한.
- 실행: fixture 작업 공간 + 가짜 S3 클라이언트, `uv run pytest tests/sandbox/test_archive.py`

## 완료 시 확인할 동작
fixture 작업 공간에서 리소스 ID별 `s3://.../<sha256>.tar.gz`와 SHA-256이 나온다.

## 결정 대기
- 소스 묶음 버킷 이름·소유·정리 규칙(D-06)

## 검증 증거
- `uv run pytest tests/sandbox/test_archive.py`: 17 통과. 같은 입력의 해시가 파일 수정 시각·생성 순서와 무관하게 같음, 원본(마스킹하지 않은 소스)으로 묶임, `.git`·`.env`·`.env.*`·`.aws`·`*.pem`·`*.key`·심볼릭 링크(파일·디렉터리)·FIFO가 들어가지 않음, 민서님의 B-01 아카이브 검사(`image_build._extract`)를 통과하고 `S3ArchiveRef` 계약(해시 이름)을 만족함, 같은 키가 있으면 업로드하지 않고 새 키면 `sources/<sha256>.tar.gz`로 올리고 버전 ID를 `ref`에 담음, `.dockerignore` 규칙(`*`, `**`, 디렉터리, 부정, 주석) 적용과 Dockerfile 항상 포함, 하위 빌드 컨텍스트를 아카이브 루트로 만듦, 다른 이름의 Dockerfile을 루트의 `Dockerfile`로 배치, 잘못된 입력(컨텍스트 이탈·없음, Dockerfile 없음·심볼릭 링크, 잘못된 리소스 ID)은 업로드 전에 거절, 파일 수 상한 초과 거절, 리소스 ID별 `source_refs`와 중복 거절, AWS CLI 저장소(없는 객체 감지, 다른 오류는 출력 없이 `STORE_ERROR`, 업로드 인자에 본문 없음)를 확인했다.
- Helm이 필요한 테스트 파일을 제외한 `pytest`와 `ruff check`·`ruff format --check` 통과. 전체 `make check`는 이 PC에 Helm이 없어 실행하지 못했다.
- 구현: `apps/worker/src/cmp_worker/source_archive.py`(`package_source`, `build_source_refs`, `AwsCliArchiveStore`). 새 의존성 없음(B-01처럼 AWS CLI와 Worker의 IAM 신원을 쓴다).
- 문서 변경: spec·plan 변경 없음(§2.4와 같은 구조).

## 한계와 미검증
- 실제 S3 버킷과 권한은 검증하지 못했다(가짜 저장소와 가짜 `aws` 실행 파일). 버킷 소유·이름·정리 규칙(D-06)은 호출부가 넘기는 인자이며 설정·Activity 연결은 하지 않았다.
- 해시는 같은 Python·zlib 환경에서 재현된다. 환경이 다르면 압축 바이트가 달라질 수 있으나 내용 해시 이름이라 정확성에는 영향이 없다(키가 달라질 뿐).
- `.dockerignore`는 자주 쓰는 규칙만 구현했다(전체 Docker 문법과 다를 수 있음). 아카이브는 메모리에 만들며 250MB 상한까지만 허용한다.
- 02의 Activity 연결과 03 작업 공간 경로를 넘기는 통합은 별도 작업이다. 실제 B-01 빌드와의 연동은 10에서 한다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리소스 ID별 source_refs 형식 반영.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
