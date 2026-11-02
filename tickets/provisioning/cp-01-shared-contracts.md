# CP-01 — 순차 개발을 위한 최소 공유 계약

## 상태
DONE — 구현·검증 완료, main 반영은 PR 병합 후. [Issue #9](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/9). 사용자 요청에 따라 병렬 운영 준비를 제외하고 필요한 공유 코드를 순차 구현한다.

## 선행 작업
Blocked by: 01, P0 정책 정렬(#7)의 main 반영

## 관련 요구사항
PROV-005, PROV-006, PROV-014, PROV-015, PROV-021

## 범위

[ADR-002](../../docs/decisions/ADR-002.md)에 따라 Sandbox/Provisioning 공유 Chart 입출력과 05/06/07의 최소 Plan·Preview·승인 대상 계약만 먼저 구현한다. 범용 Outbox/산출물 플랫폼, 승인 HTTP 라우트, Terraform/Helm 실행은 제외한다.

## 수용 기준

- [x] Plan ID·Revision·계약/템플릿 버전과 대상 식별을 공유한다.
- [x] 최소 ArtifactRef와 파일/정규화 JSON Digest의 규칙·공개 함수·fixture를 제공한다.
- [x] Preview의 실행 범위·필수 산출물·검증 오류/미해결 결정과 승인 가능 조건을 명시한다. 인프라만 준비된 Preview가 앱/DB 배포까지 승인하지 않는다.
- [x] 실행 후 생성되는 DB 주소/Secret의 허용 바인딩 위치를 표현하고, 임의 변경은 허용하지 않는다.
- [x] DB Chart Values 입력(리소스·PVC·StorageClass·Secret 이름/키) 및 렌더 산출물 계약을 확정한다. Sandbox 입력에 Production 자격 증명을 요구하지 않는다.
- [x] AWS/GCP/ON_PREM을 소비자로 삼는 정상/오류 fixture와 직렬화·Digest 변경·불완전 계획의 승인 불가 테스트를 통과한다.
- [x] 코드·검증·문서를 하나의 구현 PR로 제공하고 후속 순차 작업이 소비할 공개 경계를 기록한다. main 반영 상태는 구현 완료와 별도 기록한다.

## Test Seam
공개 계약 모델/정규화·Digest 함수/승인 가능 조건 → 직렬화 결과와 구조화 오류. 실제 저장소 접근·클라우드 Plan·승인 API·Temporal 실행을 증명하지 않는다. 사용자 합의한 선행 공통 계약 범위에서 이 seam을 사용한다.

## 구현 위치와 후속 작업

`cmp_contracts.artifact/chart/plan`, `cmp_providers.artifacts/plans/bindings`를 구현했다. [공유 계약 설명](../../specs/provisioning/shared-contracts.md)에 모델·함수와 제한을 기록한다. 새 하위 패키지나 의존성 없이 기존 패키지의 모듈로 추가했다.

후속 작업은 한 티켓씩 순차 진행한다. 다음 02에서 `PostgresChartValues`를 실제 Helm Chart로 렌더한다. 05/06은 산출물 참조와 Preview를 생성하고, 07은 이 순수 승인 대상 검증과 실제 DB 승인/인증/Temporal을 연결한다. 별도 병렬 worktree/채팅 생성은 요구하지 않는다.

## 구현 검증 (2026-11-03)

- RED → GREEN: 공개 진입점 부재, 파일 무결성 검증 부재, 렌더 산출물 역할 혼동을 먼저 확인하고 구현했다.
- 신규 공개 경계 테스트 57개 통과. 세 Provider fixture 직렬화, 파일 바이트 변조, JSON 순서·금지 타입, 누락 산출물, 변경 Revision/계정/State/이미지/Preview, scope 불일치, 바인딩 허용 위치·다른 필드 변조, Sandbox 입력을 검증했다.
- `UV_CACHE_DIR=/tmp/cmp-uv-cache make check`: lint/format 및 총 113 tests 통과. 기존 Starlette/httpx deprecation warning 1개.
- 자체 리뷰: 승인 대상 식별과 사용자 승인 권한 구분, 실제 파일 조회/소유권·활성 Revision/State drift의 호출자 책임, 불완전 계획·명세 역할, 동적 값 외 변경 차단 및 SDD 정합성 확인.
- 실제 S3/Cloud/Helm/Temporal 실행 없음. HTTP 라우트 변경 없음. OpenAPI 재생성·Temporal smoke·Cloud Apply는 비대상이다.
- docs-only Issue/PR은 생성하지 않았다. 작업 방식·계약 설명·티켓 변경은 구현 #9에 포함한다.

- `git diff --check` 및 변경 Markdown 12개 상대 링크 검사 통과. 구현 커밋 `6fe95d3`. PR에서 최종 CI/병합 상태를 추적한다.
