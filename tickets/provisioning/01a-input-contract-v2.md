# Ticket 01A — 입력 계약 v2와 Planning 출처 검증

## 상태

DONE — [Issue #1](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/1). 코드·문서·로컬 검증 완료; main 병합 여부는 PR에서 확인한다.

## 목적과 범위

사용자가 2026-11-04 확정한 리소스 ID 목록·Build 선행·Sandbox 동일 이미지 증거를 공개 v2 Planning 진입점에서 검증한다. Analysis 요구사항의 정확한 바이트 SHA, 앱별 소스·Build·Sandbox 참조/플랫폼/Digest, 선택 대상과 환경 참조를 조합해 Plan 입력을 반환한다. Build 맥락의 리소스 ID와 선택적 S3 Version ID도 지원한다.

기존 v1 Planning과 Chart 계약은 호환을 위해 유지하되 v2 증거로 승격하지 않는다. 다중 리소스 Chart 렌더, 실제 S3/업무 DB 참조 조회와 소유권, 최상위 Temporal 연결, Plan·Preview 저장, 승인, Terraform/Helm Apply는 별도 구현이다.

## 공개 Test Seam

`prepare_plan_v2(envelope, requirements_bytes=...)`의 반환 `ProvisioningSpecV2`와 안전한 `ProvisioningInputError.issues`. 빌더는 `ImageBuilder(BuildContext).build(source_uri, source_version_id=...)`의 결과와 AWS CLI 호출 계약을 확인한다. 로컬 fixture는 실제 AWS/GCP 접근 성공이나 Sandbox 테스트 성공을 증명하지 않는다.

## 수용 기준

- [x] 단일/다중 APPLICATION과 POSTGRESQL의 고유 ID·참조 그래프를 검증하고 미지원 종류·DB 확장을 거절한다.
- [x] 요구사항 바이트 SHA, Analysis→Build→Sandbox의 소스·이미지·Build 참조·플랫폼 일치를 확인한다.
- [x] Plan ID 없이 대상·`.env`/mapping·보고서·테스트 묶음 참조와 정리 상태를 보존하며 비밀값을 노출하지 않는다.
- [x] 서로 다른 리소스의 같은 Build 시도가 ECR 태그/로그에서 충돌하지 않고 선택적 S3 Version ID를 조회에 사용한다.
- [x] `make check`와 기존 v1 회귀가 통과하고 문서·프로필 실행 안내가 코드와 일치한다.

## 실행 증거와 남은 경계

- 2026-11-04: `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` — Ruff lint/format 통과, pytest **238 passed**(기존 Starlette 경고 1건). v1 회귀를 포함한다.
- `tests/test_provisioning_v2.py`에서 정상 단일/다중 앱, 세 대상 종류, 원문·참조·Digest·플랫폼·상태·설정·정리 불일치, 임의 키의 오류 노출 방지를 확인했다. `tests/test_image_build.py`에서 리소스별 태그/로그 분리와 선택적 S3 Version ID 전달을 확인했다.
- 실제 S3 객체·ECR/GCP 권한·업무 DB 소유권 조회, 다중 리소스 렌더, Temporal 인계와 클라우드 배포는 이 순수 경계 밖이며 검증하지 않았다. 로컬 클라우드 작업용 프로필 이름만 기록했고 계정 접근은 시도하지 않았다.
