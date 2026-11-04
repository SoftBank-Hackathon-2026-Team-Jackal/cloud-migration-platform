# Ticket 18 — ON_PREM Plan 게시 연결

## 상태
DRAFT — 사용자 검토 전. 2026-11-05 대화에서 15와 분리하기로 했다(15 PR 크기 억제).

## 목적
Agent(15)가 실행할 배포 작업은 승인된 ON_PREM Plan Revision에서 나온다. 지금은 Plan을 만드는 운영 코드가 없어 테스트·스크립트만 Plan을 게시한다. Planning 입력 v2에서 ON_PREM Plan Revision을 실제로 게시하는 경로를 만들어 "이전 요청 → 승인 → Agent 작업"이 이어지게 한다.

## 관련 요구사항
PROV-004, PROV-005, PROV-009, PROV-014, PROV-015, PROV-021

## 선행 작업
Blocked by: 04B, 07, 12, 12A (DONE), 15(작업을 실제로 받을 Agent)

## 현재 구현과 재사용 지점
- `cmp_providers` 04B v2 렌더: DB·앱 Values 생성
- `tests/onprem_rollout_support.py`: Chart `helm package`, Values 직렬화, `ArtifactStore.put`, `PlanManifest` 구성 — 테스트 전용
- `cmp_providers.onprem_preview.onprem_preview` (12A): 순수 함수, 운영 호출 없음
- `cmp_api.approvals.publish_plan_revision`: "Planning integration calls this later; no HTTP route" — 운영 호출 없음
- `ArtifactStore`: 로컬 Digest 디렉터리(`ARTIFACT_STORE_DIR`)만 구현

## 작업 범위 (초안)
- 포함
  - ON_PREM Plan 생성 함수: Planning 입력 v2 + 대상(Agent ID·등록 cluster UID·Namespace·StorageClass) → v2 렌더 → Chart 패키징 → 산출물 저장 → `PlanManifest` → `onprem_preview` → `publish_plan_revision`. 같은 입력의 재실행은 같은 Revision을 다시 만들지 않는다.
  - 대상 검증: Agent가 등록돼 있고 폐기되지 않았으며 cluster UID가 등록 값과 같아야 게시한다.
  - 호출 경계: `provisioning` queue Activity(후속 MigrationWorkflow 연결용)와 운영자용 실행 경로(스크립트 또는 API) 중 하나 이상.
- 제외: AWS/GCP Plan 게시(같은 공백이 있으나 별도), S3 ArtifactStore, 프론트 Preview 화면, MigrationWorkflow 전체 연결.

## 수용 기준 (초안)
- [ ] 정상 입력으로 게시한 ON_PREM Revision의 Preview가 12A 계약을 통과하고 승인 후 12의 Agent 작업이 만들어진다.
- [ ] 등록되지 않은·폐기된 Agent, cluster UID 불일치, 렌더 실패는 게시하지 않고 구조화 오류를 반환한다.
- [ ] 같은 입력의 재실행이 중복 Revision을 만들지 않고, 입력이 바뀌면 새 Revision이 된다.
- [ ] 산출물·Preview·로그에 Secret 원문이 없다.

## Test Seam 후보
SQLite 업무 DB + 로컬 ArtifactStore + 실제 Helm 패키징으로 생성 함수 → 승인 → `execute_on_prem_plan`(가짜 Agent)까지. 15 완료 후 k3d에서 실제 Agent가 이 Plan의 작업을 처리하는지 확인.

## 검토 시 정할 것
- 호출 경계(Activity / 운영자 API / 스크립트)와 Planning 담당 경계
- 재실행 판정 기준(입력 Digest)
- 산출물 저장소를 로컬로 유지할지 S3까지 포함할지

## 관련 문서
- [12](12-k3s-approved-rollout.md) · [12A](12a-onprem-preview.md) · [04B](04b-shared-render-v2.md) · [15](15-agent-runtime-install.md) · [공유 계약](../../specs/provisioning/shared-contracts.md)

## 설계 변경 기록
- 2026-11-05: 15 범위 검토 중 "플랫폼이 발급하는 작업"의 출처가 비어 있음을 확인해 분리했다.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
