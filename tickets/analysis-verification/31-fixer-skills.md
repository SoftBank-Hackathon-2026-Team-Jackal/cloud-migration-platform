# Ticket 31 — Fixer가 상황에 맞는 Skill을 필요할 때 불러 쓰게 연결

## 상태
IN_PROGRESS — 구현·자동 테스트 완료(2026-11-06). 실제 Claude 호출 확인은 비용 승인 후 1회. Issue 번호는 사용자가 발행한 뒤 기록한다.

## 목적
Fixer는 Skill 도구(`load_skill`)가 없어서, 클라우드 종속 코드를 대상에 맞게 고치는 방법 같은 지침을 줄 수 없었다(Mapper·Tester는 이미 쓴다). Fixer가 **특정 상황에서만, 그 상황에 맞는 Skill만** 불러 읽게 한다. 이번 범위는 연결뿐이고 새 Skill 내용(예: `storage-s3`)은 다루지 않는다.

## 관련 요구사항
AV-007, AV-015, AV-020

## 선행 작업
Blocked by: 14A, 20 (병합됨)

## 설계
- 상황별 규칙은 Skill 옆의 `skills/fixer-triggers.yaml`에 둔다: `skill`, `failure_classes`(비우면 SPEC·CODE·BUILD 전부), `reason_patterns`(실패의 사유 코드에서 찾는 정규식). Skill을 추가할 때 규칙도 함께 추가하며 Fixer 코드는 바꾸지 않는다.
- `select_skills(skills_root, failure_class, reasons)`가 이번 실패에 맞는 Skill 이름을 규칙 순서로 한 번씩 고른다. INFRA·RENDER, 규칙 파일 없음·깨짐, Skill 폴더 없음이면 아무것도 고르지 않는다(실패 시 닫힘). Tester의 `test-authoring`은 절대 고르지 않는다.
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
- [x] 쓰기 가능 경로, 파일 크기·개수 제한, 테스트·`.env` 금지는 그대로다.

## 테스트 전략
- Test Seam: 순수 함수 `select_skills`(규칙 × 실패), 가짜 LLM의 `propose_fix`(Skill을 부르거나 안 부름), `sbx_fix` 연결. 잡는 것: 상황별 선택, 허용 범위, 프롬프트 비주입. 못 잡는 것: 실제 모델이 Skill을 적절히 쓰는지(비용 승인 후 1회).
- 실행: `uv run pytest tests/agents tests/sandbox/test_publish_activities.py -q`, 이후 `make check`

## 문서
plan §2.11(Fixer와 Skill), tasks.md를 같은 PR에서 갱신한다.

## 결정 대기
- 실제 Claude에서 Skill 사용 여부 확인(비용 승인 후, 예상 호출 수를 말씀드리고 진행).
