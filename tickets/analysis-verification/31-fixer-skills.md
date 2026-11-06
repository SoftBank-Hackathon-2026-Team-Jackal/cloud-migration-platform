# Ticket 31 — Fixer가 상황에 맞는 Skill을 필요할 때 불러 쓰게 연결

## 상태
DONE — PR #252·#253 병합, Issue #251 (2026-11-06). 미확인: 실제 Claude 호출로 Skill 사용 확인 1회(비용 승인 후)

## 목적
Fixer는 Skill 도구(`load_skill`)가 없어서, 클라우드 종속 코드를 대상에 맞게 고치는 방법 같은 지침을 줄 수 없었다(Mapper·Tester는 이미 쓴다). Fixer가 **특정 상황에서만, 그 상황에 맞는 Skill만** 불러 읽게 한다. 이번 범위는 연결뿐이고 새 Skill 내용(예: `storage-s3`)은 다루지 않는다.

## 관련 요구사항
AV-007, AV-015, AV-020

## 선행 작업
Blocked by: 14A, 20 (병합됨)

## 설계
- 상황별 규칙은 Skill 옆의 `skills/fixer-triggers.yaml`에 둔다: `skill`, `failure_classes`(비우면 SPEC·CODE·BUILD 전부), `reason_patterns`(실패의 사유 코드에서 찾는 정규식). Skill을 추가할 때 규칙도 함께 추가하며 Fixer 코드는 바꾸지 않는다.
- 규칙은 선택 필드 `targets`(AWS, GCP, ON_PREM)로 대상을 제한할 수 있다. Skill을 **나누거나 나누지 않을 수 있게** 한 설계다: `targets` 없는 Skill 하나가 모든 대상을 맡고 대상별 차이는 Skill 본문("대상별 차이")에 쓰거나, 대상마다 Skill을 나누고 각각 `targets`를 둔다. 둘은 함께 쓸 수 있고 한 실패에서 Skill이 여러 개 선택될 수 있다. `targets`가 있는 규칙은 대상을 모를 때 맞지 않는다.
- `select_skills(skills_root, failure_class, reasons, target_provider=None)`가 이번 실패에 맞는 Skill 이름을 규칙 순서로 한 번씩 고른다. INFRA·RENDER, 규칙 파일 없음·깨짐, Skill 폴더 없음이면 아무것도 고르지 않는다(실패 시 닫힘). Tester의 `test-authoring`은 절대 고르지 않는다.
- `propose_fix`는 고른 이름이 있을 때만 `load_skill`을 등록하고, 고른 Skill만 읽을 수 있다. Skill 내용은 프롬프트에 미리 넣지 않고 이름 목록만 알려 준다(모델이 필요하면 불러 읽음, 비용 상한 ADR-004). 읽은 Skill은 실행 기록(`skills_loaded`)에 남는다.
- Sandbox 수정 단계(`sbx_fix`)가 기존 Skill 위치(`skills_root`)와 실패 분류·사유로 선택해 연결한다.
- 첫 규칙은 `network`(Sandbox가 막은 호출: `EGRESS_BLOCKED:`, `MIGRATE_SOURCE_CALLED:`) 하나다. 다른 Skill의 규칙은 그 Skill을 쓸 근거가 생길 때 추가한다.

## 작업 범위
- 포함: `cmp_agents/fixer_skills.py`, `propose_fix`의 Skill 인자, 프롬프트 한 항목, `sbx_fix` 연결, `skills/fixer-triggers.yaml`, 테스트.
- 제외: 새 Skill 작성(`storage-s3` 등), 의존성 선언 파일 허용 정책, 수정 후 보안 재판정(29), 실제 Claude 호출.

## 수용 기준
- [x] 실패의 분류·사유가 규칙과 맞으면 그 Skill 이름이 선택되고, 안 맞으면 선택되지 않는다. 규칙은 실패 분류를 제한할 수 있다.
- [x] 선택된 Skill만 `load_skill`로 읽을 수 있고 읽은 Skill은 `skills_loaded`에 남는다. 선택되지 않은 Skill은 읽히지 않는다.
- [x] 선택된 Skill이 없거나 Skill 위치가 없으면 도구가 붙지 않는다(기존 동작과 같음). INFRA·RENDER는 선택하지 않는다.
- [x] Skill 내용은 프롬프트에 미리 들어가지 않고 이름 목록만 들어간다.
- [x] Tester의 `test-authoring`은 Fixer에게 선택되지 않는다. 존재하지 않는 Skill을 가리키는 규칙은 무시된다.
- [x] `sbx_fix`가 규칙에 맞는 실패에서 Skill을 제공하고, 맞지 않으면 제공하지 않는다.
- [x] 실제로 들어 있는 규칙 파일은 실제 Skill만 가리키고 정규식이 유효하다.
- [x] 규칙에 `targets`를 두면 그 대상일 때만 선택되고, 대상을 모르면 선택되지 않는다. `targets`가 없는 규칙은 모든 대상에서 선택된다. 알 수 없는 대상 값이 든 규칙 파일은 깨진 것으로 보고 아무것도 주지 않는다.
- [x] 한 실패에서 Skill이 둘 이상 선택될 수 있다(원본 서비스용과 특정 대상용).
- [x] 대상 Provider가 `AnalysisInput.target_provider`와 `AnalysisReport.target_provider`로 전달된다. API 시작은 요청의 `target.provider`, CLI 시작은 `--planning`의 대상 선택에서 채우고 두 시작 입력은 같다. 알 수 없는 값은 거절되고 없으면 `None`이다.
- [x] `sbx_fix`가 분석 결과에서 대상 Provider를 읽어 Skill 선택에 넘긴다. 대상 한정 규칙은 그 대상일 때만 선택되고, 다른 대상이거나 Provider를 모르면 선택되지 않는다.
- [x] 쓰기 가능 경로, 파일 크기·개수 제한, 테스트·`.env` 금지는 그대로다.

## 테스트 전략
- Test Seam: 순수 함수 `select_skills`(규칙 × 실패), 가짜 LLM의 `propose_fix`(Skill을 부르거나 안 부름), `sbx_fix` 연결. 잡는 것: 상황별 선택, 허용 범위, 프롬프트 비주입. 못 잡는 것: 실제 모델이 Skill을 적절히 쓰는지(비용 승인 후 1회).
- 실행: `uv run pytest tests/agents tests/sandbox/test_publish_activities.py -q`, 이후 `make check`

## 문서
plan §2.11(Fixer와 Skill), tasks.md를 같은 PR에서 갱신한다.

## 대상 Provider 전달 (구현 완료)
- 조사 결과(2026-11-06): `TargetSelectionV2.provider`는 `PlanningSettings`(시작 입력의 `planning`)에 있고 Sandbox 입력과 `sbx_fix` payload에는 없었다. Sandbox가 아는 대상 정보는 `capabilities`(StorageClass)뿐이다.
- 전달 경로: `AnalysisInput.target_provider` → `AnalysisReport.target_provider` → `sbx_fix`가 이미 받는 `analysis_report_ref`에서 읽어 `select_skills(..., target_provider)`에 넘긴다. API 시작은 요청의 `target.provider`, CLI 시작(`build_start_input`)은 `--planning`의 `target_selection.provider`로 채워 두 시작 입력이 계속 같다(기존 동등성 테스트). 값이 없으면 `None`이고, 그러면 대상 한정 규칙은 맞지 않는다.
- **Workflow 변경 없음**: 부모 Workflow의 `assemble_sandbox_input` 입력을 바꾸지 않았다(`workflow.patched` 불필요). 계약에는 선택 필드만 더했다.

## 결정 대기
- 실제 Claude에서 Skill 사용 여부 확인(비용 승인 후, 예상 호출 수를 말씀드리고 진행).
