# Ticket 37 — 레드팀을 Sandbox Workflow와 수정 루프에 연결

## 상태
IN_REVIEW — Issue #352. 연결 코드와 가짜 Activity·가짜 실행기 시험, `make check`(3680개 통과) 완료(로컬). 실제 모델 호출·도구 실행기·승인 화면은 이 티켓 범위 밖이라 이 티켓 시점에는 레드팀이 실제로 공격하지 않았다(`RedTeamKit`이 없으면 `REDTEAM_NOT_CONFIGURED`). 이후 [티켓 38](38-redteam-execution-wiring.md)이 키트를 Sandbox 워커에 연결했다. 실제 Claude 호출 1회는 확인됐다(티켓 38 참조).

## 목적
티켓 33~36이 따로 만든 격리·안전장치·Agent 루프·수정 루프를 `SandboxVerificationWorkflow`에서 실제로 호출되게 잇는다. 이 연결이 없으면 네 티켓의 코드는 시험에서만 쓰인다.

## 관련 요구사항
AV-024, AV-015, AV-019, [ADR-008](../../docs/decisions/ADR-008.md) 결정 9·10·12·13, R3·R4·R8

## 선행 작업
Blocked by: 33, 34, 35, 36 (병합됨)

## 범위
- 포함
  - Workflow 입력 `redteam`(강도, 시간 상한, 대상 리소스 ID, 경로 목록, 턴 상한). 호스트·URL·주소 필드는 없다
  - Activity `sbx_redteam_request`(승인 화면에 보일 내용과 답을 기다릴 수 있는 시간), `sbx_redteam`(한 번의 승인된 공격 또는 이전 Finding만 다시 확인)
  - Workflow 단계: 시험을 통과한 Sandbox에서 공격 전에 사용자 승인 Signal(`approve_redteam`·`decline_redteam`)을 기다린다. 답이 없으면 Sandbox 만료 안에서 시간 초과로 건너뛴다
  - Finding이 열려 있으면 `sbx_classify`(SECURITY) → `sbx_fix`(마스킹한 요약만 전달) → 재빌드 → 같은 시험 → 그 Finding의 재현 요청만 새 Digest에서 다시 실행
  - 보안 수정은 기능 수정과 별도로 3회(R4). 소진돼도 표시와 기록만 하고 승인을 막지 않는다(R3)
  - 승인은 실행마다 새로 받는다(R8, 한 번 쓴 승인은 거절)
- 제외: 실제 모델 호출(`decide`), 도구 실행기 안의 실제 도구 실행(`dispatch`), 관찰을 Finding으로 바꾸는 판단(`detect`·`reproduces`), 승인 화면·API, 이벤트(`sandbox.*`) 발행, 데모 리허설, 경로 목록의 출처 확정

## 수용 기준
- [x] 스위치가 꺼진 입력은 Activity도 결과 필드도 바뀌지 않는다(기존 Workflow 시험 회귀 없음)
- [x] 시험 통과 → 승인 요청 → 승인 → 공격 한 번 → Sandbox 정리 → 게시 순서가 Workflow 이력에 남는다
- [x] 거절·시간 초과·시간 부족·시작 거부는 공격하지 않고 실행 결과에 사유를 남기며 실행은 통과로 끝난다
- [x] 열린 Finding이 수정 → 새 빌드 → 재확인으로 닫힌다. 재확인은 그 Finding의 요청만 보낸다
- [x] 끝까지 열린 Finding은 `STILL_OPEN`으로 표시되고 실행은 막히지 않는다
- [x] 보안 수정이 기능 수정 횟수를 쓰지 않는다
- [x] 보호를 낮추는 수정은 기존 `SECURITY_LOWERED_BY_FIX` 경로로 `NEEDS_REVIEW`가 된다
- [x] 증거에 비밀이 남으면 그 Finding은 저장하지 않고 `SECRET_IN_EVIDENCE`로 표시한다
- [x] 죽은 실행기는 Finding을 `FIXED`로 만들지 않는다
- [x] 실제 모델·실제 도구·격리 k3d에서 1회 실행(사용자 확인)
- [ ] 승인 화면·API 합의 후 `approve_redteam` 연결

## Test Seam
- Workflow: 가짜 `sbx_*` Activity(`tests/sandbox/test_redteam_workflow.py`)
- Activity 본문: 가짜 `RedTeamKit`(모델·도구·판단)과 기존 안전장치의 가짜 Service·Isolation(`tests/sandbox/test_redteam_stage.py`)
- 놓치는 것: 실제 클러스터에서의 격리·초기화·외부 차단, 실제 모델의 도구 선택, 승인 화면

## 구현 요약
- `cmp_worker.redteam_stage`: `RedTeamInput`, `RedTeamKit`, `request_for`, `run_stage`. 키트가 없으면 `REDTEAM_NOT_CONFIGURED`. 안전장치 순서(격리 증명 → 외부 차단 → 작업 → 해제 → 데이터 복원)는 `RedTeamRunner`가 그대로 지킨다
- Finding id는 `카테고리|경로|재현 요청`의 해시여서 이미지가 바뀌어도 같은 약점은 같은 id를 가진다. 재확인에서 요청이 실패하면 `FIXED`가 아니라 열린 채로 남는다
- Workflow는 `workflow.patched("redteam-v1")` 뒤에서만 동작한다. 입력에 `redteam`이 없는 기존 이력은 그대로 재생된다
- 수정 루프 횟수: `self.attempt - sec_attempts >= 3`. 보안 수정은 `sec_attempts`로 따로 센다
- 공격 Activity는 재시도하지 않는다(`maximum_attempts=1`): 한 번 쓴 승인은 재시도에서 거절되기 때문이다
- Fixer 입력 `FixInput.findings`는 범주·심각도·경로·요약·재현 요약(마스킹)만 담고 재현 요청과 증거 참조는 담지 않는다

## 티켓과 달라진 점·알려진 한계
- **기존 옵트인(`SandboxSettings.redteam`·동의 참조)과 파이프라인은 아직 이어지지 않았다.** 이 티켓의 입력 `SandboxInput.redteam`은 같은 동의 참조(`consent_ref`)를 요구하지만, `MigrationWorkflow`가 `SandboxSettings`에서 이 입력을 만들어 주지는 않는다(경로 목록·대상 리소스의 출처가 ADR-008 R7에서 미정). 그래서 전체 파이프라인 실행은 레드팀 없이 기존과 똑같이 돈다
- **승인은 Signal 하나로 시작한다.** 화면·API는 승인 화면 담당과 합의 전이다(ADR-008 결정 9). `redteam_state` Query가 승인 화면에 보일 내용을 준다
- **(티켓 38에서 해소)** 아래 두 항목(키트 배선, 설정→입력)은 [티켓 38](38-redteam-execution-wiring.md)(#355)에서 채웠다.
- **`RedTeamKit`을 만드는 운영 배선이 없다.** `build_sandbox_loop`는 아직 키트를 넘기지 않아 운영 Worker에서 이 단계는 `SKIPPED`로 남는다. 모델 호출·도구 실행·판단은 다음 작업이다
- **승인 장부는 메모리형이다**(`InMemoryApprovalLedger`). Worker가 여러 대이면 업무 DB 기반으로 바꿔야 한다
- **재확인 승인도 매번 받는다.** 수정 뒤 확인마다 사용자에게 답을 요청하므로, 승인 한 번으로 재확인까지 묶을지는 사용자 결정이 필요하다
- 같은 Digest의 요구사항 수정이면 재확인은 같은 Digest에서 다시 실행한다(재빌드가 없기 때문)
- 이벤트(`sandbox.attempt` 등)는 보안 시도에 대해 발행하지 않는다. 화면 계약이 정해진 뒤 추가한다
- 공격 Activity에는 Heartbeat가 없다. 시간 상한은 `activity` 타임아웃(상한 + 15분)과 Agent 루프의 시간 예산이 지킨다

## 문서
spec AV-024, runbook, plan §2.8, tasks.md를 같은 PR에서 갱신했다. 제품 정책·API 계약·책임 경계는 바뀌지 않았다.

## 검증 (2026-11-07, macOS)
- `tests/sandbox/test_redteam_workflow.py` 12개, `tests/sandbox/test_redteam_stage.py` 16개
- `make check`: 3680 passed, 32 skipped (기존 Windows 한정 실패는 해당 없음)
- 미검증: 실제 Temporal 서버 `make smoke-temporal`, 실제 모델, 실제 k3d
