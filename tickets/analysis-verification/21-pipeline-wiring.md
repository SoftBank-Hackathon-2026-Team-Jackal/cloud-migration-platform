# Ticket 21 — 분석 → Sandbox 루프 → 발행 연결과 통합 테스트

## 상태
IN_PROGRESS — 착수(설계 조사 중), Issue #115

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
- [ ] 분석 결과(보고서·요구사항·검토 기록)가 Sandbox 입력으로 조립되어 자식 루프가 실제 경로로 시작된다.
- [ ] 검토 Signal 이후 루프가 통과하면 최종 분석 버전과 요구사항 참조가 발행된다.
- [ ] 문자열 입력과 기존 이력 replay는 유지된다.
- [ ] 같은 입력의 통합 테스트가 k3d에서 통과한다.
- [ ] 변경 범위와 replay 결과를 PR 설명에 기록해 민서님께 공유한다.

## 테스트 전략
- 제안 Test Seam: Temporal time-skipping + 실제 k3d, 가짜 LLM. 잡는 것: 부품 사이의 입력·출력 불일치, 부모의 자식 시작·결과 처리. 못 잡는 것: 실제 모델의 품질, 실제 Build.
- 실행: `uv run pytest tests/integration/sandbox -m k3d -k pipeline`, `make smoke-temporal`

## 완료 시 확인할 동작
`AnalysisInput`을 넣으면 분석 → 검토 → Sandbox 검증 → 발행이 한 Workflow 실행으로 이어진다.

## 결정 대기
- 저장소 연결(D-02, D-06): `ContentRef`류(요구사항·보고서·검토 기록)는 분석 워커와 Planning이 같은 저장소 디렉터리를 가리키게 설정하면 되고(`analysis_artifact_store_dir`를 Planning의 `artifact_store_dir`와 같게), `S3ArchiveRef`류(소스 묶음·테스트 묶음)는 S3 업로드가 필요하다. 이 티켓의 통합 테스트는 Build를 주입하므로 S3 없이 돈다.
- 부모 입력 모양 변경에 대한 민서님 확인.

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
