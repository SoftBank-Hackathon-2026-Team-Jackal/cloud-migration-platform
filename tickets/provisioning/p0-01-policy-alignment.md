# P0-01 — 정책 정렬과 병렬 착수 조건

## 상태
DONE — 코드·문서·로컬 검증 완료; main 반영은 PR 병합 후

GitHub Issue: [#7](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/7)

Blocked by: 01 (PR #6 main 반영 완료)

## 범위와 수용 기준

- [x] 사용자 합의한 P0 축소·승인 유지 결정을 ADR-002 및 product/spec/plan/architecture/티켓에 동기화한다.
- [x] AWS/GCP 기존 네트워크 재사용 요청을 P0 미지원 오류로 거절한다. ON_PREM 기존 K3s와 신규 네트워크 경로는 유지한다.
- [x] CP-01 선행 작업, 02/05/06/07의 의존성·파일 소유권·분기/병합 순서 및 미정 착수 조건을 명시한다.
- [x] RED/GREEN, make check, 문서 링크/diff, 코드·문서 리뷰 증거와 커밋/PR을 기록한다.

## Test Seam

공개 prepare_plan/adapt에서 AWS/GCP EXISTING 모드 거절을 검사한다. 기존 CREATE/ON_PREM 정상 및 오류 검사를 회귀로 실행한다. 실제 Terraform/Helm·클라우드 작업은 수행하지 않는다.

## 문서 및 코드

[ADR-002](../../docs/decisions/ADR-002.md), [병렬 작업 규칙](../../specs/provisioning/parallel-work.md), [CP-01](cp-01-shared-contracts.md)을 참조한다. 기존 입력 모델은 호환 읽기를 유지하지만 공개 Adapter가 `UNSUPPORTED_P0_NETWORK_MODE`로 거절한다.

이번 티켓은 공통 계약 전체 구현이나 네 티켓 구현을 대신하지 않는다. 별도 채팅 생성 없음.

## 검증 증거 (2026-11-03)

- RED: AWS/GCP EXISTING 거절 테스트 2개가 오류 미발생으로 실패함을 확인.
- GREEN: 기존 네트워크 입력은 읽되 Adapter에서 P0 미지원 오류 반환. 변경된 제품 정책에 맞게 기존 허용 테스트를 갱신.
- `UV_CACHE_DIR=/tmp/cmp-uv-cache make check`: lint/format 통과, 56 passed (Provisioning 53 + scaffold 3), 기존 Starlette/httpx 경고 1개.
- 변경 Markdown 18개 상대 파일 링크 검사 및 `git diff --check` 통과.
- 자체 리뷰: 기존 네트워크 파싱과 P0 지원 구분, ON_PREM 범위 보존, 승인 기록과 Outbox 구분, 정책/구현 완료 구분, 파일 담당과 후속 의존성 확인.
- HTTP/Temporal 실행 코드 변경 없음. OpenAPI 재생성·Temporal smoke 비대상. 실제 자원 생성 없음.
- CP-01 및 02/05/06/07은 미구현 상태를 유지한다. 정책 합의만으로 병렬 준비 완료라고 표시하지 않는다.
