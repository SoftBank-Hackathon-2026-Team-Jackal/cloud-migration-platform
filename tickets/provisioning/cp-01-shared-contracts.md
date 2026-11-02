# CP-01 — 병렬 구현을 위한 최소 공유 계약

## 상태
TODO — 사용자 승인된 공통 준비 범위, 구현 미착수. GitHub Issue는 착수 시 중복 검색 후 생성/연결한다.

## 선행 작업
Blocked by: 01, P0 정책 정렬(#7)의 main 반영

## 관련 요구사항
PROV-005, PROV-006, PROV-014, PROV-015, PROV-021

## 범위

[ADR-002](../../docs/decisions/ADR-002.md)에 따라 Sandbox/Provisioning 공유 Chart 입출력과 05/06/07의 최소 Plan·Preview·승인 대상 계약만 먼저 구현한다. 범용 Outbox/산출물 플랫폼, 승인 HTTP 라우트, Terraform/Helm 실행은 제외한다.

## 수용 기준

- [ ] Plan ID·Revision·계약/템플릿 버전과 대상 식별을 공유한다.
- [ ] 최소 ArtifactRef와 파일/정규화 JSON Digest의 규칙·공개 함수·fixture를 제공한다.
- [ ] Preview의 실행 범위·필수 산출물·검증 오류/미해결 결정과 승인 가능 조건을 명시한다. 인프라만 준비된 Preview가 앱/DB 배포까지 승인하지 않는다.
- [ ] 실행 후 생성되는 DB 주소/Secret의 허용 바인딩 위치를 표현하고, 임의 변경은 허용하지 않는다.
- [ ] DB Chart Values 입력(리소스·PVC·StorageClass·Secret 이름/키) 및 렌더 산출물 계약을 확정한다. Sandbox 입력에 Production 자격 증명을 요구하지 않는다.
- [ ] AWS/GCP/ON_PREM을 소비자로 삼는 정상/오류 fixture와 직렬화·Digest 변경·불완전 계획의 승인 불가 테스트를 통과한다.
- [ ] 코드·문서가 main에 반영되고 네 작업의 동일 기준 커밋·수정 파일 담당이 기록된다.

## Test Seam
공개 계약 모델/정규화·Digest 함수/승인 가능 조건 → 직렬화 결과와 구조화 오류. 실제 저장소 접근·클라우드 Plan·승인 API·Temporal 실행을 증명하지 않는다. 사용자 합의한 선행 공통 계약 범위에서 이 seam을 사용한다.

## 파일 담당

`packages/contracts/src/cmp_contracts/plan.py`, `chart.py`, `packages/providers/src/cmp_providers/artifacts.py`, `tests/test_plan_contracts.py`, `tests/test_chart_contracts.py`, `tests/fixtures/contracts/`를 제안 위치로 예약한다. 기존 `provisioning.py`는 필요한 최소 연결만 수정한다. 디렉터리/의존성 변경 시 패키지 설정·lockfile은 이 담당자가 통합한다.

## 병렬 작업 인계

[parallel-work.md](../../specs/provisioning/parallel-work.md)의 준비 체크리스트를 만족한 뒤 각 티켓을 별도 worktree/브랜치로 시작한다. CP-01 구현 중에는 후속 작업자가 공통 모델을 별도로 복제하지 않는다.
