# Ticket 21 — 분석 → Sandbox 루프 → 발행 연결과 통합 테스트

## 상태
IN_PROGRESS — 구현 완료(실제 k3d `cmp-sandbox`에서 검증), 리뷰 대기, Issue #115

## 목적
분석, Sandbox 갱신 루프, 최종 발행을 하나의 흐름으로 잇고 통합 테스트로 검증한다. 각 부품은 따로 검증되었지만 부모 Workflow가 이어 주지 않아 실제 경로가 한 번도 끝까지 돌지 않았다.

## 관련 요구사항
AV-001, AV-015, AV-017, AV-018

## 선행 작업
Blocked by: 14C, 19, 20 (모두 병합됨)

## 작업 범위
- 포함: `MigrationWorkflow`가 dict 입력(`AnalysisInput` 포함)이면 분석 자식 결과(보고서·요구사항·검토 기록)를 받아 Sandbox 자식에 넘기는 경로(문자열 입력은 기존 동작, `workflow.patched`로 이력 보존), 분석 결과 → `SandboxInput` 조립 Activity(표준 테스트 묶음 생성·저장, 소스 작업 공간, DB 이미지, KEEP·원본 호스트), 빌드 호출 자리(D-12 전까지 미리 빌드된 이미지 주입), 통합 테스트(가짜 LLM + 로컬 저장소 + k3d 공개 이미지로 분석 → 검토 Signal → 루프 → 발행)
- 제외: Planning 인계(15), 실제 Build 배치(D-12), 데모 앱·실제 Claude 실행(16), 승인 API·화면

## 수용 기준
- [x] 분석 결과(보고서·요구사항·검토 기록)가 Sandbox 입력으로 조립되어 자식 루프가 실제 경로로 시작된다.
- [x] 검토 Signal 이후 루프가 통과하면 최종 분석 버전과 요구사항 참조가 발행된다.
- [x] 문자열 입력과 기존 이력 replay는 유지된다.
- [x] 같은 입력의 통합 테스트가 k3d에서 통과한다.
- [ ] 변경 범위와 replay 결과를 PR 설명에 기록해 민서님께 공유한다. (PR을 올릴 때 채운다)

## 테스트 전략
- 제안 Test Seam: Temporal time-skipping + 실제 k3d, 가짜 LLM. 잡는 것: 부품 사이의 입력·출력 불일치, 부모의 자식 시작·결과 처리. 못 잡는 것: 실제 모델의 품질, 실제 Build.
- 실행: `uv run pytest tests/integration/sandbox -m k3d -k pipeline`, `make smoke-temporal`

## 완료 시 확인할 동작
`AnalysisInput`을 넣으면 분석 → 검토 → Sandbox 검증 → 발행이 한 Workflow 실행으로 이어진다.

## 구현 요약
- **부모(`workflows/migration.py`):** `run(data: str | dict)`. 문자열은 기존 scaffold 그대로이고, dict(`{"project_id", "analysis": AnalysisInput, "sandbox": SandboxSettings}`)면 분석 자식에 `AnalysisInput`을 넘기고, 결과로 `assemble_sandbox_input` Activity(분석 큐)를 불러 만든 입력으로 Sandbox 자식을 시작한다. 통과한 뒤 분석·요구사항 참조와 발행 결과(`published`)를 Query의 `pipeline` 필드에 둔다. 문자열 입력의 명령 순서는 그대로여서 `workflow.patched`가 필요 없고, 기존 이력 replay 테스트가 모두 통과한다.
- **분석 자식(`workflows/analysis.py`):** 실제 경로의 결과에 `source`(작업 공간 경로·커밋)를 추가했다. 분석을 실제 경로로 돌리는 곳은 지금 테스트뿐이라 기존 이력에 영향이 없다.
- **조립(`cmp_worker/sandbox_input.py`, `AnalysisActivities.assemble_sandbox_input`):** 순수·결정적 함수. 분석 결과(요구사항·보고서·검토 기록 참조, 소스 작업 공간)와 `SandboxSettings`(실행 ID, DB 이미지(D-11 전 `postgres:16` 다이제스트 고정), StorageClass, 환경 변수 이름, 원본 호스트, 표준 테스트용 쓰기·읽기 경로)로 Sandbox 입력을 만들고, 요구사항에서 표준 테스트 묶음을 만들어 저장한다. 오브젝트 스토리지가 `KEEP`이면 해당 S3 호스트를 프록시 허용 목록에 넣는다.
- **표준 테스트 묶음의 보정:** 고정된 헬스 테스트가 요구사항의 헬스 경로를 직접 품으면 Fixer가 경로를 고친 뒤에도 옛 경로를 계속 요구해 실패했다. 그래서 헬스 테스트는 `{{health_path}}`를 쓰고 실행기가 대상의 현재 경로를 넣는다(`run_suite(variables=...)`). 묶음 바이트는 그대로이고 대상만 바뀐다. 표준 테스트에는 `covers`도 채웠다(헬스는 `environment-config`, DB 쓰기·재시작 테스트는 `postgres`).

## 결정 (이전 "결정 대기")
- 저장소(D-02, D-06): `ContentRef`류는 분석 워커의 `analysis_artifact_store_dir`를 Planning의 `artifact_store_dir`와 같은 디렉터리로 두는 것으로 해결하고(코드 변경 없음), `S3ArchiveRef`류는 15번과 실제 빌드 전에 S3 버킷 1개를 만든다(승인 필요).
- 부모 입력 모양 변경은 PR 설명으로 민서님께 공유한다.

## 검증 증거
- 단위·Workflow(`tests/analysis/test_sandbox_input.py`, `tests/analysis/test_pipeline_wiring.py`, `tests/testsuite`): 실제 분석 + 대본 Sandbox Activity로 분석 결과가 Sandbox 입력으로 조립되고 통과·실패가 부모에 반영되는 것, 문자열 입력과 이력이 그대로인 것, 이력에 소스·비밀이 없는 것을 확인.
- 실제 k3d: `uv run pytest tests/integration/sandbox/test_pipeline_k3d.py -m k3d`(약 2분). 로컬 저장소 → 실제 분석 → 검토 Signal → Sandbox 루프(첫 시도는 요구사항의 포트·헬스 경로가 틀려 SPEC 실패 → 대본 Fixer가 정정 → 같은 Sandbox 갱신 → 통과) → 최종 분석 버전 발행(`environment-config` 행이 `SANDBOX_VERIFIED`, 발행된 요구사항이 고친 버전)이 하나의 부모 Workflow 실행으로 이어지고 Sandbox가 정리된다.

## 한계와 미검증
- 모델과 이미지 빌드는 가짜다(대본 응답, 공개 podinfo 다이제스트). 실제 Claude로 돌린 결과와 실제 Build는 16번 전에 확인한다.
- 코드 수정(CODE) 경로와 재승인 경로는 이 통합 테스트에 없다(앞선 가짜 Activity 테스트로만 확인).
- 실제 앱(데모 앱)과 비공개 저장소·이미지 접근은 16번의 조건이다.
- 승인 API·화면은 없다(검토 Signal은 분석 자식에 직접 보낸다).

## 남은 결정
- 저장소 연결(D-02, D-06): `ContentRef`류(요구사항·보고서·검토 기록)는 분석 워커와 Planning이 같은 저장소 디렉터리를 가리키게 설정하면 되고(`analysis_artifact_store_dir`를 Planning의 `artifact_store_dir`와 같게), `S3ArchiveRef`류(소스 묶음·테스트 묶음)는 S3 업로드가 필요하다. 이 티켓의 통합 테스트는 Build를 주입하므로 S3 없이 돈다.
- (해결됨, 위 결정 참조)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [ADR-003](../../docs/decisions/ADR-003.md) · [ADR-004](../../docs/decisions/ADR-004.md)

## 설계 변경 기록
- 2026-11-05: 신설. 14C와 20 병합 뒤 부품이 이어지지 않는 문제를 별도 티켓으로 분리.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
