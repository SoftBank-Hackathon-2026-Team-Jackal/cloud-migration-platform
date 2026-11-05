# Ticket 15 — Planning 인계(입력 v2)

## 상태
IN_PROGRESS — Issue #141 (사용자 검토 완료, 2026-11-05)

## 목적
분석·빌드·검증 결과를 Planning 입력 v2(`PlanningInputRegistration`)로 조립하고 Planning이 읽는 곳에 등록해, 부모 Workflow가 검증을 통과한 뒤 바로 계획 단계로 이어지게 한다. 같은 테스트 묶음을 Production에서 다시 돌릴 수 있게 문서도 남긴다.

## 관련 요구사항
AV-018

## 선행 작업
Blocked by: 14C, 21, 22, 25(빌드 증거). D-02는 결정됨(로컬 불변 저장소 공유). D-10(Production 테스트 시점)은 문서화 범위만 영향.

## 작업 범위
- 포함
  - `planning_handoff`(순수): 통과한 Sandbox 결과·`AnalysisReport`·`RequirementsV2` 참조·25의 `BuildAttempt`·`test_suite_ref`·시작 설정에서 `SandboxVerification`을 만들고 01의 `project_planning`으로 `PlanningRequestV2`를 투영, `PlanningInputRegistration`(대상 설정 포함)으로 감싼다. 투영 로직은 새로 만들지 않는다.
  - 검증: 등록 전에 `prepare_plan_v2(envelope, requirements_bytes=...)`로 한 번 검사해 거절 사유를 코드로 반환
  - 등록 활동 `register_planning_input`: 민서님의 `register_planning_input`(DB 등록 함수)을 Worker의 세션으로 호출(HTTP·관리자 토큰을 쓰지 않는다). 같은 입력이면 같은 Digest, 변경되면 새 Digest
  - 부모 Workflow: Sandbox 통과 직후, 계획 게시(`publish_migration_plan`) 전에 등록 활동 호출(`workflow.patched("planning-handoff-v1")`, 실제 경로 입력일 때만). 문자열(scaffold) 입력과 기존 이력은 그대로
  - 시작 설정 `planning`(대상 선택·대상 설정·환경 참조). `.env` 참조는 ENV-02(민서님)가 생기기 전까지 시작 설정으로 받는 임시 경로
  - 무효 처리: 검증 뒤 대상 선택 버전·요구사항·분석 버전이 바뀌면 이전 검증을 넘기지 않는다
  - 문서: `run_suite`를 다른 `base_url`·인증 참조로 실행하는 예시, 배포 직후에는 준비 검사만·데이터 이전 후 테스트 묶음 실행이라는 순서
- 제외: Planning·Preview 구현과 대조 규칙(민서님), Production 테스트 실행 시점(D-10), 승인 화면·API

## 수용 기준
- [x] 조립 결과가 `prepare_plan_v2` 검증을 통과한다.
- [x] 리소스별 Build Digest와 `validated_workloads`의 Digest가 같다. 서로 다르거나 빌드 증거가 없으면(`PrebuiltImages` 경로) 등록하지 않고 `BUILD_EVIDENCE_MISSING` 등 코드로 실패한다.
- [x] 검증 뒤 대상 선택 버전이 바뀌면 이전 검증을 넘기지 않는다.
- [ ] 등록 뒤 `publish_migration_plan`이 `PLANNING_INPUT_MISSING` 없이 계획을 게시한다. (등록 내용을 Planning의 `load_planning_input`이 읽는 것까지 확인. 실제 게시는 아직 확인하지 못함)
- [x] 비밀 값(`.env` 원문, 토큰)이 등록 내용에 없다. 이름과 참조만 있다.
- [x] 문자열 입력(scaffold)과 기존 부모 이력이 그대로 replay된다.
- [x] `run_suite` 사용 예시와 실행 시점 순서가 문서화된다.

## 테스트 전략
- 제안 Test Seam: 조립 함수(순수, 고정된 증거 입력), 등록 활동(민서님 함수 + 임시 DB), 부모 Workflow 이력(가짜 활동, patched 켜짐·꺼짐 두 경우). 잡는 것: 계약 정합성, Digest 일치, 무효 처리, 이력 호환. 못 잡는 것: 실제 Production 배포.
- 실행: `uv run pytest tests/sandbox/test_handoff.py tests/test_provisioning.py tests/analysis -q`, `make check`, 가능하면 `make smoke-temporal`

## 영향이 있는 경계 (민서님께 통보)
- 부모 `workflows/migration.py`에 등록 활동 호출 1곳 추가(patched). 기존 이력은 영향 없음.
- 민서님의 `register_planning_input` 함수를 Worker에서 호출(함수 변경 없음).
- `.env` 참조는 임시 경로이며 ENV-02가 생기면 그쪽으로 교체.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 계약 협의안 v2 형식과 배포 후 테스트 시점 반영. v2 동기화로 투영 대상을 `PlanningRequestV2`·`prepare_plan_v2`로 확정.
- 2026-11-04(전체 재검토): 투영 함수는 01에서 구현됨. 15는 저장소 연결과 무효 처리만 담당하도록 범위 조정.
- 2026-11-05: 등록 경로를 확인함 — Planning은 DB의 `PlanningInputRegistration`을 읽으므로 인계는 그 등록이다. 빌드·테스트 묶음의 S3 증거가 필요해 25(빌드 연결)를 선행으로 추가하고, 두 티켓을 같은 PR로 진행한다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인 (`make check` 1601 통과)
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록 (실제 AWS·smoke-temporal 미실행)
