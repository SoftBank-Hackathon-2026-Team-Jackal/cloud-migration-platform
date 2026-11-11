# Ticket 34 — 레드팀 안전장치와 Finding 계약

## 상태
IN_REVIEW — Issue #333. 단위·서비스 시험과 lint 통과(로컬). 실제 클러스터 확인은 사용자가 확인했다. 전체 pytest는 CI에서 대기. 선행: 33(병합됨). 결정: [ADR-008](../../docs/decisions/ADR-008.md) R2(강도), R6(동의 문구).

## 목적
레드팀 검증이 사용자의 실제 `.env`·데이터·외부 서비스에 피해를 주지 않도록 **안전장치**를 먼저 만들고, 결과를 담는 **Finding 계약**을 정한다. 이 티켓은 실행 도구를 정하지 않는다.

## 관련 요구사항
AV-024(신규), AV-020, P-10, P-12

## 선행 작업
Blocked by: 33, 11B(`snapshot_baseline`·`reset_test_data`, 병합됨), 13(`external_cut`, 병합됨)

## 범위
- 포함
  - **외부 차단:** 레드팀 실행 중 기존 `external_cut`으로 외부 egress를 끊고, 끝나면 복구한다. 끊을 수 없으면 시작하지 않는다(`REDTEAM_EXTERNAL_NOT_CUT`)
  - **강도 제한:** `PASSIVE` / `ACTIVE` 두 단계와 제외 대상(자원 고갈형)을 설정 모델로 고정한다. `ACTIVE`는 기준선이 있을 때만 허용하고 종료 시 `reset_test_data`를 반드시 실행한다(`REDTEAM_BASELINE_MISSING`)
  - **마스킹:** 증거는 저장 전에 `cmp_scanners.masking.mask_text`와 앱의 실제 비밀 값 치환을 통과시킨다. 마스킹 후에도 실제 `.env` 값이 남으면 그 Finding은 저장하지 않고 `SECRET_IN_EVIDENCE`로 표시한다
  - **Finding 계약:** 카테고리, 심각도, 영향받은 엔드포인트, 재현 요청 요약, 마스킹된 증거 참조(ArtifactRef), **이미지 Digest, 묶음 SHA-256, 강도, 실행 시각**. 원문 요청·응답 전문은 Workflow 이력에 넣지 않고 참조만 둔다
  - **실행 전 승인(R8):** 시작 직전 승인 단계를 둔다. 승인 요청에 대상·강도·시간 상한·초기화·외부 차단 사실을 담고, 승인자·시각·강도·상한을 기록한다. 승인 기록이 없으면 `REDTEAM_NOT_APPROVED`로 시작하지 않는다. 승인 시점에 남은 Sandbox 시간을 다시 확인한다. 기존 확정 Signal 게이트 방식 재사용 후보
  - 만료 안전: 남은 Sandbox 시간(`expires-at`)이 부족하면 시작하지 않는다(`REDTEAM_TIME_INSUFFICIENT`)
- 제외: 도구 선택·실행, LLM 조율(35), 수정 연계(36), 승인 차단 게이트

## 수용 기준
- [ ] 외부 차단 실패·기준선 없음·시간 부족이면 어떤 시험도 시작하지 않고 사유 코드로 남는다
- [ ] `ACTIVE` 실행 뒤 테스트 데이터가 기준선으로 돌아간다(성공·실패·시간 초과 모두)
- [ ] 마스킹 시험: 알려진 키 패턴·`.env` 값이 증거와 Finding 어디에도 남지 않는다
- [ ] Finding에 이미지 Digest와 묶음 해시가 항상 들어가고, 없으면 계약 검증에서 거절된다
- [ ] Workflow 이력·로그에 요청·응답 원문이 없다
- [ ] 옵트인이 꺼져 있으면 기존 결과·해시가 변하지 않는다
- [ ] 승인 없음·승인 거절·승인 대기 중 시간 초과일 때 어떤 시험도 시작하지 않고 Pod와 정책도 만들어지지 않는다. 승인은 해당 실행 하나에만 유효하다(재사용 불가)
- [ ] 승인 요청에 보이는 강도·상한과 실제 실행 설정이 다르면 시작하지 않는다(승인 뒤 설정 변경 방지)

## Test Seam
- 순수: Finding 모델 왕복, 마스킹 함수, 강도·기준선 판정
- 서비스: `FakeRunner`로 외부 차단 → 실행 → 복구·초기화 순서와 실패 경로
- 실제: k3d에서 외부 차단·초기화 동작 확인
- 놓치는 것: 새로운 형태의 비밀 값 패턴(마스킹 한계)

## 문서
spec AV-024, plan §6, runbook, tasks.md를 같은 PR에서 갱신한다.

## 구현 요약
- `cmp_worker.redteam_safeguards`
  - **계약:** `Finding`(이미지 Digest·묶음 SHA-256·강도·실행 시각 필수, 엔드포인트는 앱 안의 경로만, 요약 필드 길이 제한, 알 수 없는 필드 거절), `make_finding`, `FindingSummary`/`summarize`(Workflow 이력에는 개수·id만).
  - **증거:** `store_evidence`가 앱의 실제 비밀 값(원문·URL 인코딩·base64)을 치환하고 `mask_text`를 적용한 뒤에만 저장한다. 치환 뒤에도 값이 남으면(예: 대문자로 바뀐 경우) 저장하지 않고 `SECRET_IN_EVIDENCE`. 6자 미만 값은 설정값(`true`, `8080`)과 구분할 수 없어 치환하지 않는다.
  - **시작 전 확인:** `check_start` — `REDTEAM_NOT_APPROVED`, `REDTEAM_APPROVAL_MISMATCH`(실행 ID·강도·상한이 다름), `REDTEAM_TIME_INSUFFICIENT`(상한 + 120초 여유보다 적게 남음, 시작 시점 기준), `REDTEAM_BASELINE_MISSING`(ACTIVE만), `REDTEAM_APPROVAL_USED`. 승인은 마지막에 소진하므로 다른 이유로 거절되면 그대로 쓸 수 있다.
  - **실행 래퍼:** `RedTeamRunner.run` — 격리 증명(33) → 외부 차단 → 작업 → 차단 해제·실행기 제거 → (ACTIVE만) 데이터 복원. 격리 증명 전에 외부를 끊으면 프록시가 닿지 않는 것처럼 보여 증명이 무의미해지므로 순서를 고정했다. 작업이 실패해도 복원하고, 복원 실패는 숨기지 않는다(`__context__`로 원인 유지). 외부를 끊지 못하면 `REDTEAM_EXTERNAL_NOT_CUT`, 작업은 시작하지 않는다.
  - **승인 요청:** `approval_request`가 대상·강도·시간 상한·초기화·외부 차단 사실을 이름과 사실로만 담는다.
- `SandboxService.baseline_exists`: 복원할 기준선이 있는지.

## 티켓과 달라진 점
- 승인을 **받는** 경로(확정 Signal 게이트, 화면, API)는 구현하지 않았다. 이 티켓은 승인 기록의 모양과 검증까지이고, 받는 쪽은 승인 화면 담당과 합의가 필요하다. 승인 소진 기록은 `ApprovalLedger` Protocol이고 구현은 메모리 안(`InMemoryApprovalLedger`)뿐이다. 워커가 여러 대이면 업무 DB 구현이 필요하다.
- Workflow·Activity에는 아직 연결하지 않았다(33과 같음).
- 티켓에 `cmp_agents.masking`이라고 적었던 것은 실제 위치인 `cmp_scanners.masking`으로 바로잡았다.

## 검증 (2026-11-07, Windows)
- `tests/sandbox/test_redteam_safeguards.py` 34개, 33의 36개와 관련 기존 Sandbox 시험(`test_deploy`·`test_network`·`test_network_probe`·`test_update`)을 포함해 111개 통과, `ruff check`·`ruff format --check` 통과.
- 전체 pytest는 이 PC(Windows)의 `fcntl`·`cp949` 문제로 CI에서 확인한다.

## 미검증
- 실제 k3d에서 외부 차단과 데이터 복원의 순서(래퍼는 가짜 서비스로 검증, 각 동작은 기존 티켓 11B·13의 시험이 다룸).
- 승인을 받는 경로와 다중 워커의 승인 소진 기록.
- 새로운 형태의 비밀 값 패턴(마스킹의 한계).
