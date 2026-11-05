# Ticket 18 — Plan 생성·게시와 Preview 조회 연결 (세 대상)

## 상태
TODO — [Issue #98](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/98). 착수 전 검토 필요. 2026-11-05 Provisioning 공백 점검에서 범위를 ON_PREM 전용에서 세 대상과 Preview 조회까지 넓혔다.

## 목적
승인(07)과 실행(08~12)은 게시된 Plan Revision이 있어야 시작되는데, 지금 Plan을 만들고 게시하는 운영 코드가 없다. 테스트와 `scripts/`만 Plan을 게시한다. 또 승인 요청에 `preview_digest`가 꼭 필요하지만 Preview를 돌려주는 API가 없어 승인자가 Preview를 볼 수 없다. Planning 입력 v2에서 대상별 Plan·Preview를 만들어 게시하고, 승인자가 Preview를 조회해 승인하는 경로를 만들어 "이전 요청 → Plan·Preview → 승인 → 실행"을 잇는다.

## 관련 요구사항
PROV-004, PROV-005, PROV-007, PROV-008, PROV-009, PROV-014, PROV-015, PROV-021

## 선행 작업
Blocked by: 01A, 04B, 05, 06, 07, 12, 12A, 15 (DONE)

## 현재 구현과 재사용 지점
- `cmp_worker.provisioning_v2.prepare_plan_v2`: Planning 입력 v2 검증(순수 함수)
- `plan_aws_infrastructure`·`plan_gcp_infrastructure`(05·06): `scripts/`·테스트에서만 호출
- `cmp_providers.onprem_preview.onprem_preview`(12A): 운영 호출 없음
- `tests/onprem_rollout_support.py`: Chart 패키징·Values 직렬화·`ArtifactStore.put`·`PlanManifest` 구성 — 테스트 전용
- `cmp_api.approvals.publish_plan_revision`: 운영 호출 없음, HTTP route 없음
- `MigrationWorkflow`의 `planning`(`prepare_plan`)·`provisioning`(`provision_target`) 단계: scaffold("No Terraform plan generated")

## 작업 범위
- 포함
  - 대상별 Plan 생성 Activity(`provisioning` queue): Planning 입력 v2 + 대상 설정 → AWS/GCP는 Terraform Plan·Preview(05·06), ON_PREM은 v2 렌더·Chart 패키징·`onprem_preview`(12A) → 산출물 저장 → `PlanManifest` → `publish_plan_revision`. 같은 입력의 재실행은 같은 Revision을 다시 만들지 않는다.
  - ON_PREM 대상 검증: Agent가 등록돼 있고 폐기되지 않았으며 cluster UID가 등록 값과 같을 때만 게시한다.
  - Preview 조회 API: 승인자 인증(07)으로 `plan_id`·`revision`의 Preview 본문과 `preview_digest`를 반환한다. 비밀값은 포함하지 않는다.
  - `MigrationWorkflow`의 `planning`·`provisioning` 두 단계를 실제 Activity(Plan 게시 → 승인 대기 → 실행 결과)로 교체한다. 다른 단계는 건드리지 않는다.
  - SDD·OpenAPI·Temporal 흐름 문서 갱신
- 제외: S3 ArtifactStore(21에서 결정), 프론트 Preview 화면, 다른 팀 단계(분석·Sandbox·데이터·트래픽)의 연결, `.env` 값 주입(20)

## 수용 기준
- [ ] 세 대상 각각 정상 입력으로 게시한 Revision의 Preview가 05·06·12A 계약을 통과하고, 승인 후 08·10·12 실행이 시작된다.
- [ ] Preview 조회 API가 승인 권한이 있는 승인자에게만 Preview와 Digest를 돌려주고, 그 Digest로 승인할 수 있다.
- [ ] 등록되지 않은·폐기된 Agent, cluster UID 불일치, 렌더·Plan 실패는 게시하지 않고 구조화 오류를 반환한다.
- [ ] 같은 입력의 재실행이 중복 Revision을 만들지 않고, 입력이 바뀌면 새 Revision이 된다.
- [ ] `MigrationWorkflow`가 Plan 게시·승인 대기·실행 결과를 실제로 거치고, 승인 거절·만료 시 실행하지 않는다.
- [ ] 산출물·Preview·API 응답·로그·Temporal 이력에 Secret 원문이 없다.

## Test Seam 후보
- SQLite 업무 DB + 로컬 ArtifactStore + 실제 Helm 패키징으로 ON_PREM 생성 Activity → Preview API → 승인 → `execute_on_prem_plan`(가짜 Agent).
- AWS/GCP는 05·06의 가짜 Terraform 실행기로 Plan 생성·게시까지.
- Temporal 테스트 환경으로 `MigrationWorkflow` 두 단계(승인·거절·만료).
- 놓치는 것: 실제 클라우드 Plan(05·06에서 확인됨), 실제 서버 실행(21).

## 검토 시 정할 것
- 대상 설정(Agent ID·Namespace·StorageClass·리전 등)을 누가 어떤 입력으로 주는지와 Planning 담당 경계
- 재실행 판정 기준(입력 Digest)
- `MigrationWorkflow` 단계 교체를 Migration 소유자와 확인할지

## 관련 문서
- [05](05-aws-preview.md) · [06](06-gcp-preview.md) · [07](07-approval-gate.md) · [12](12-k3s-approved-rollout.md) · [12A](12a-onprem-preview.md) · [04B](04b-shared-render-v2.md) · [공유 계약](../../specs/provisioning/shared-contracts.md)

## 설계 변경 기록
- 2026-11-05: 15 범위 검토 중 "플랫폼이 발급하는 작업"의 출처가 비어 있음을 확인해 분리했다.
- 2026-11-05: 공백 점검에서 AWS/GCP도 같은 공백이고 Preview 조회 API가 없음을 확인해 범위를 넓혔다.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
