# Ticket 09 — 소스 묶음 생성과 S3 업로드

## 상태
DRAFT — 사용자 검토 전

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
- [ ] 같은 입력을 두 번 묶으면 같은 SHA-256이 나온다.
- [ ] `.git`, `.env`, 심볼릭 링크, 경로 이탈 항목이 들어가지 않는다.
- [ ] 만든 아카이브가 B-01 아카이브 검사(경로·링크·`.env`·크기)를 통과한다.
- [ ] 마스킹본이 아니라 원본으로 묶인다.
- [ ] 같은 해시가 이미 있으면 업로드하지 않는다.

## 테스트 전략
- 제안 Test Seam: `package_source(workspace)`가 돌려주는 바이트·해시·URI. 잡는 것: 재현성·제외 규칙. 못 잡는 것: 실제 S3 권한.
- 실행: fixture 작업 공간 + 가짜 S3 클라이언트, `uv run pytest tests/sandbox/test_archive.py`

## 완료 시 확인할 동작
fixture 작업 공간에서 리소스 ID별 `s3://.../<sha256>.tar.gz`와 SHA-256이 나온다.

## 결정 대기
- 소스 묶음 버킷 이름·소유·정리 규칙(D-06)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 리소스 ID별 source_refs 형식 반영.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
