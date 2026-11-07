# Ticket 36 — Finding을 수정 루프에 연결하고 재검증

## 상태
IN_REVIEW — Issue #335. 단위 시험과 lint 통과(로컬). 실제 Fixer·클러스터 연계는 대기. 선행: 34, 35(병합 전, 같은 브랜치 계열). 결정: ADR-008 R3(게이트, 표시만)·R4(별도 시도 횟수) 확정.

## 목적
찾은 Finding을 기존 수정 루프(14A·14B, Fixer)로 넘겨 앱을 고치고, 고친 이미지로 **같은 시험만 다시** 해서 해소를 확인한다. 보안을 낮추는 방향의 수정은 거절한다.

## 관련 요구사항
AV-024, AV-015, AV-019, P-10, P-12

## 선행 작업
Blocked by: 34, 35, 14B·31(병합됨)

## 범위
- 포함
  - `classify_failure`에 보안 분류를 추가하고 Fixer 입력으로 Finding 요약(마스킹본)을 전달
  - Finding 상태 `OPEN → FIXED / STILL_OPEN / REGRESSED`, 재검증은 해당 Finding의 재현 요청만 대상으로 한다
  - 새 빌드의 Digest에 대해서만 재검증 결과를 인정한다(P-10). 다른 Digest의 결과는 재사용하지 않는다
  - 시도 횟수: R4 결정에 따라 보안 수정 전용 횟수를 둔다
  - 수정이 인증·접근 제한·암호화 같은 기존 보호를 약화시키면 거절하고 사람 검토로 보낸다(P-12)
  - 미해결 Finding은 표시와 승인 기록까지(D-17). 승인 차단은 R3 결정 전까지 하지 않는다
- 제외: 승인 차단 게이트, Production 재검증

## 수용 기준
- [ ] Finding 하나가 수정 → 새 Digest → 재검증으로 `FIXED`가 되는 흐름이 가짜 실행기에서 닫힌다
- [ ] 수정이 해소하지 못하면 `STILL_OPEN`으로 남고 횟수가 소진되면 사유와 함께 종료한다
- [ ] 이전 Digest의 재검증 결과는 새 Digest에 적용되지 않는다
- [ ] 보호를 약화시키는 수정은 거절된다
- [ ] 기능 수정 루프의 기존 동작과 횟수 규칙에 회귀가 없다
- [ ] 데모 리허설: 심어 둔 취약점 목록(정답지)과 Finding을 비교해 발견율과 소요 시간을 3회 이상 측정해 기록하고, 정답지는 Agent 입력에 쓰이지 않았음을 확인한다. 취약 이미지가 실제 대상에 배포되지 않는 흐름(수정 후 배포)을 확인한다

## Test Seam
- 순수: 상태 전이, 분류 순서, 약화 판정
- 서비스: 가짜 Fixer·가짜 실행기로 수정·재검증 루프
- 실제: 사용자 승인 후 격리 k3d에서 의도적 결함이 있는 데모 앱으로 1회
- 놓치는 것: 여러 Finding이 서로 영향을 주는 경우

## 문서
plan §2.8·§2.9, spec AV-024, tasks.md를 같은 PR에서 갱신한다.

## 구현 요약 (`cmp_worker.redteam_fixloop`, `cmp_worker.failure_classifier`)
- **상태 전이** (`RevalidationLedger`): Finding마다 마지막으로 확인한 Digest와 상태를 기록한다. `record(finding_id, digest, reproduces=...)`가 재현되면 이전에 `FIXED`였던 경우만 `REGRESSED`, 아니면 `STILL_OPEN`. 재현되지 않으면 `FIXED`. `status_for(finding_id, digest)`는 **그 Digest에서 확인한 상태만** 돌려준다 — 다른 Digest에서는 `None`(P-10, 다른 Digest의 결과를 재사용하지 않음).
- **재검증** (`revalidate`): 그 Finding의 재현 요청만 새 Digest에 다시 돌린다. 이미 그 Digest에서 확인된 상태가 있으면 다시 돌리지 않는다.
- **보호 약화 거절** (`ProtectionSignals`, `weakens_protection`): 인증 제거·접근 제어 확대·암호화 제거 중 하나라도 있으면 `SECURITY_LOWERED_BY_FIX:<종류>`로 거절하고 재검증을 시도조차 하지 않는다(P-12). 기존 `security_guard.py`(29-B, 분석 측정값 비교)와 코드 네임스페이스(`SECURITY_LOWERED_BY_FIX`)는 같지만, 이 모듈은 레드팀 Finding 전용 신호를 받는 별도 함수다.
- **시도 횟수** (`MAX_SECURITY_FIX_ATTEMPTS = 3`): `sandbox_events.MAX_ATTEMPTS`(기능 수정 루프)와 분리된 상수다(R4).
- **루프** (`run_fix_loop`): 수정 → 약화 검사 → 재검증을 반복한다. `FIXED`·`REGRESSED`·거절 중 하나가 나오면 즉시 멈추고, 상한에 닿으면 `STILL_OPEN`으로 멈춘다.
- **`classify_failure` 확장**: `AttemptSignals.open_security_findings`(기본 빈 튜플, 기존 호출부에 영향 없음)가 있으면 `SECURITY` 분류와 `FINDING:<id>` 사유를 낸다. INFRA보다는 아래, RENDER·BUILD·CODE·SPEC보다는 위에 둔다(환경이 깨진 게 아니면 Finding이 있다는 사실이 다른 분류보다 더 구체적인 원인이다).

## 티켓과 달라진 점
- **게이트는 R3 확정대로 표시만이다.** `run_fix_loop`는 거절·상한 소진 모두 호출자에게 결과로 돌려줄 뿐, Provisioning 승인을 막는 코드는 이 티켓에 없다(§범위 "제외"와 일치).
- **Fixer·실행기 연동은 아직 가짜 함수다.** `fix`와 `reproduce`는 호출하는 쪽이 준다. 실제 14A/14B Fixer 입력으로 Finding 요약을 넘기는 연결과, 실제 레드팀 실행기(33)로 재현을 다시 돌리는 연결은 Workflow 통합 때 한다. 통합은 [티켓 37](37-redteam-workflow-connection.md)(#352)에서 했고, 실제 Fixer(`sbx_fix`)·재빌드·재확인 연결까지 가짜 Activity로 닫았다. 실제 클러스터 실연계는 아직이다.
- **보호 약화 판정은 신호 기반이다.** 실제 코드 diff를 파싱해 `ProtectionSignals`를 만드는 부분은 이 티켓 범위가 아니다(기존 `security_guard.py`도 같은 선례: 플레인텍스트 패턴만 본다).
- **데모 리허설 수용 기준(발견율·소요 시간 3회 측정, 배포 흐름 확인)은 실제 클러스터와 Fixer 연동이 끝난 뒤에만 가능하다.** 아직 하지 않았다.

## 검증 (2026-11-07, Windows)
- `tests/sandbox/test_redteam_fixloop.py` 21개, 기존 `test_classifier.py` 포함 `tests/sandbox` 전체(`test_workflow.py` 제외, 기존 `fcntl` 문제) 398개 통과·10개 skip. 실패 17개는 전부 기존 `fcntl`/`cp949` Windows 문제(이 변경과 무관, `test_worker_wiring.py`·`test_publish.py` 등)로 확인했다.
- `ruff check`, `ruff format --check` 통과.

## 미검증
- 실제 Fixer 연동과 Finding 요약 전달.
- 실제 레드팀 실행기로의 재검증 디스패치.
- 데모 리허설(발견율·소요 시간 측정, 수정 후 배포 확인).
