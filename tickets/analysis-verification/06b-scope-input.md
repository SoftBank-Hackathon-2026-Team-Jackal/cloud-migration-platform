# Ticket 06B — 확정된 분석 범위를 분석 입력으로 연결

## 상태
IN_PROGRESS — 구현·자동 테스트 완료(2026-11-06, 전체 2598 통과, `make openapi` 반영). 실제 클러스터 후보로 1회 확인과 웹 계약 합의, 사용자 검토가 남음. GitHub Issue 번호는 사용자가 발행한 뒤 tasks.md에 기록한다.

## 목적
웹의 "이전 범위 선택"(티켓 56, ADR-005)이 확정한 정보를 분석이 실제로 쓰게 한다. 지금은 확정 기록(`scope_confirmations`)을 읽는 코드가 없고, API로 시작한 분석 입력(`AnalysisInput`)에는 저장소·커밋만 있어 `cluster_name`·`credential_ref`가 비어 인프라 스캔이 `AWS_SCAN_NOT_CONFIGURED`로 건너뛰어진다. 프론트가 값을 직접 넣지 않고, API가 저장된 확정 기록에서 서버 쪽으로 채운다.

## 관련 요구사항
AV-001, AV-005, AV-017, AV-020

## 선행 작업
Blocked by: 06 (병합), 티켓 56 (Provisioning, 병합). 시작 요청 계약에 `scope_id` 한 필드를 더하는 일은 상헌님(웹 `rest.schema.json`)과 맞춘다. 필드는 선택이라 기존 요청은 그대로 동작한다.

## 작업 범위
- 포함
  - 계약(`cmp_contracts.analysis`): `ConfirmedScope`(`scope_id`, 선택 정보, 포함 후보 목록, 제외 후보 ID), `AnalysisInput.region`·`AnalysisInput.scope` 선택 필드, `AnalysisReport.scope` 선택 필드.
  - 시작 요청: `MigrationRequest.scope_id`(선택, `scope-<16자리 hex>`). `target` 없이 주면 거절한다.
  - API(`start_request.py`): `scope_id` → 확정 기록 → 분석 기록 → 원본 연결을 읽어 `AnalysisInput`을 채운다. `credential_ref`는 원본 연결의 프로필 이름, `cluster_name`·`region`은 선택한 값, `role_arns`는 **포함된** `IAM_ROLE` 후보의 ARN, `scope`는 확정 내용.
  - 거절 코드(값은 담지 않음): `SCOPE_NOT_FOUND`, `SCOPE_CONNECTION_NOT_FOUND`, `SCOPE_CONNECTION_KIND_MISMATCH`(원본 AWS 연결이 아님), `SCOPE_CONNECTION_NOT_READY`(연결·역할 미확인), `SCOPE_NOT_APPLICABLE`(`target` 없음).
  - 워커: 분석 AWS 세션이 입력의 `region`을 우선 쓰고 없으면 워커 설정을 쓴다. 분석이 만든 `AnalysisReport`에 입력의 `scope`를 그대로 싣는다.
- 제외: 포함·제외를 스캔 필터와 매핑의 `MIGRATE`/`KEEP` 구분에 쓰는 일(후속), `scope_id` 없는 시작의 처리 정책(결정 대기), 웹 화면, 범위 선택 API 자체(티켓 56, 민서 담당).

## 수용 기준
- [x] `scope_id`가 있는 시작은 `AnalysisInput`에 `credential_ref`(원본 연결의 프로필), `cluster_name`, `region`, `role_arns`, `scope`가 채워진다. 프론트가 보낸 ARN·프로필 이름은 쓰이지 않는다.
- [x] `role_arns`에는 **포함된** `IAM_ROLE` 후보의 ARN만 들어간다. 제외된 후보, `arn`이 없는 후보, 다른 종류의 후보는 들어가지 않는다.
- [x] 없는 `scope_id`, 원본이 아닌 연결, 확인되지 않은 연결, `target` 없는 요청은 코드만 담은 422로 거절되고 아무것도 시작하지 않는다.
- [x] `scope_id`가 없으면 시작 입력이 지금과 완전히 같다(기존 테스트 그대로 통과).
- [x] 워커가 입력의 `region`으로 AWS 세션을 만든다. 없으면 워커 설정을 쓴다.
- [x] 분석 결과(`AnalysisReport`)에 확정된 범위가 실린다. 범위 없는 기존 보고서는 읽히고 직렬화도 그대로다(`scope`가 `None`이면 바이트가 같다).
- [x] 비밀 값(자격 증명)이 입력·기록에 새로 들어가지 않는다. 프로필 이름과 ARN만 다룬다.

## 테스트 전략
- 제안 Test Seam: ① 순수 함수 `scope_analysis_fields(scope, credential_ref)`(후보 → `role_arns` 규칙), ② 기존 `test_web_start_request.py`와 같은 FastAPI TestClient + 가짜 Temporal + SQLite로 시작 요청 → Workflow 입력 확인. 잡는 것: 입력 조립, 거절 코드, 하위 호환. 못 잡는 것: 실제 클러스터에서 만든 후보(실제 계정 1회 확인은 06과 함께, 승인 필요).
- 실행: `uv run pytest tests/test_scope_start_input.py tests/analysis tests/test_web_start_request.py tests/test_scope_selection.py -q`, 이후 `make check`

## 문서
spec AV-005 비고(범위 입력), tasks.md, 웹 계약(`MigrationRequest.scope_id`)은 `make openapi`로 갱신·검토한다.

## 결정 대기
- `target`이 있는 실제 시작에서 `scope_id`를 필수로 할지(지금은 선택: 없으면 인프라 스캔이 조용히 빠진다). 필수로 하면 웹과 맞춰야 한다.
- 웹 `rest.schema.json`의 `scope_id` 필드 이름·위치(상헌님).
- 확정한 후보를 스캔 필터와 매핑 구분에 쓰는 규칙(후속 티켓).
