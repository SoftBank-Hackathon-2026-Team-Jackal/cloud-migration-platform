# Ticket 24 — 실행 도구·입력 검증·만료 Sandbox 정리·Tester 연결

## 상태
DONE — PR #139 병합, Issue #136

## 목적
실제 실행 전에 필요한 운영 도구와 안전장치를 갖춘다. (1) 실행 중인 Sandbox를 보고 지우는 스크립트와 입력 검증, (2) 워커가 죽어 남은 만료 Sandbox를 주기적으로 지우는 방법, (3) 만들어 두기만 한 Tester를 Sandbox 루프에 연결하되, 원본 쪽 테스트 자원(D-09)이 없으면 모델을 부르지 않는다.

## 관련 요구사항
AV-011, AV-012, AV-020

## 선행 작업
Blocked by: 12, 14B, 22 (모두 병합됨). 원본 쪽 테스트 자원은 D-09 결정 대기이므로 이 티켓에서는 인터페이스와 가짜로만 검증한다.

## 작업 범위
### A. 실행 도구와 입력 검증
- 포함: `scripts/sandbox_ctl.py`의 `status`(전용 클러스터의 `cmp-sbx-*` 네임스페이스, 실행 ID, 만료 시각, 만료 여부)와 `cleanup`(만료된 것만, `--all`은 확인 입력 후), 소스 확보 입력 검증(`requested_commit`이 `-`로 시작하는 값, 제어 문자, 공백을 포함하면 git을 부르기 전에 거절. 저장소 주소도 같은 규칙), 시작 입력 단계에서도 같은 검사
- 제외: 브랜치를 바꾸는 기능, Temporal Workflow 변경

### B. 만료 Sandbox 주기 정리
- 포함: 이미 있는 `SandboxService.cleanup_expired`를 쓰는 단일 실행 진입점(`sandbox_ctl.py cleanup`)과 주기 실행 안내(cron·launchd 예시), 정리 결과(지운 네임스페이스 수·이름)를 비밀 없이 출력, 전용 컨텍스트가 아니면 거부
- 제외: Temporal Schedule·새 Workflow(부모 이력과 replay에 영향), 여러 클러스터 지원

### C. Tester 연결
- 포함: Sandbox 루프에 AI 테스트 단계 추가 — 첫 적용이 성공한 뒤 한 번, 설정 `ai_tests`가 켜져 있고 원본 실행기(`origin_runner`)가 주입된 때만 `generate_tests` → `validate_tests`(네 조건) → `pin_suite`로 새 묶음을 저장하고 이후 검증에 쓴다. 원본 실행기가 없으면 모델을 부르지 않고 `NOT_CALLED:ORIGIN_UNAVAILABLE`을 기록한다. 거절 사유(`ORIGIN_UNAVAILABLE`, `NO_VALUE_ASSERTION`, `PASSES_WITHOUT_DEPENDENCY` 등)는 결과에 남는다. Workflow 변경은 `workflow.patched`로 감싸 기존 이력을 지키고, 비용은 기존 한도(예약·정산)를 그대로 쓴다.
- 제외: 실제 원본 실행기 구현(D-09), 묶음을 S3에 올리기

## 수용 기준
- [x] `sandbox_ctl.py status`가 전용 컨텍스트의 Sandbox만 보여 주고 다른 컨텍스트·네임스페이스는 건드리지 않는다.
- [x] `sandbox_ctl.py cleanup`은 만료된 것만 지우고(`--all`은 확인 후), 결과를 이름으로 출력하며 비밀은 출력하지 않는다. 같은 명령을 반복해도 안전하다.
- [x] `-`로 시작하거나 제어 문자·공백이 있는 커밋·저장소 값은 git 호출 전에 코드로 거절된다. 정상 SHA·브랜치 이름·태그는 그대로 동작한다.
- [x] `ai_tests`가 꺼져 있으면 지금과 똑같이 동작하고 기존 Workflow 이력이 replay된다.
- [x] `ai_tests`가 켜져 있어도 원본 실행기가 없으면 모델을 부르지 않고 이유 코드를 남긴다(비용 0).
- [x] 원본 실행기가 있으면 네 조건을 통과한 테스트만 묶음에 더해지고, 새 묶음 해시로 이후 검증이 실행되며, 거절된 테스트와 이유가 결과에 남는다.
- [x] Tester 입력에 Fixer 출력이 들어가지 않는 성질이 유지된다.

## 테스트 전략
- 제안 Test Seam
  - A: 입력 검증(순수 함수), 스크립트의 명령 구성(주입한 실행기, 전용 컨텍스트 고정), 출력(비밀 없음)
  - B: `cleanup_expired`를 주입한 시계와 가짜 클러스터로 호출 결과 확인
  - C: 활동(Activity) 단위로 가짜 모델과 가짜 원본·대상 실행기, 그리고 Workflow replay(기존 이력 + 새 단계 꺼짐)
- 잡는 것: 입력 우회, 오삭제, 비용 없는 건너뜀, 이력 호환. 못 잡는 것: 실제 원본 환경에서의 AI 테스트 품질(D-09 이후 실측).
- 실행: `uv run pytest tests/sandbox tests/agents tests/testsuite`, 정적 검사 `uv run ruff check .`, Workflow를 건드리므로 가능하면 `make smoke-temporal`
- 실제 k3d 확인(선택): `status`와 `cleanup`을 전용 클러스터에서 한 번 실행

## 문서
tasks.md 표와 변경 기록, runbook(상태·정리 사용법, 주기 실행, `ai_tests` 설명), spec의 AV-012 비고(원본 실행기 없으면 호출하지 않음)를 이 티켓의 PR에서 함께 갱신한다.
