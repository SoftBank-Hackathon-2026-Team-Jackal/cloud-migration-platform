# Ticket 14C — 최종 발행·재승인·수정 패치 산출물

## 상태
IN_PROGRESS — 구현 완료(공개·재승인은 실제 k3d와 가짜 Activity, 패치는 실제 git), 리뷰 대기, Issue #111

## 목적
통과한 결과만 최종 분석 결과로 발행하고, 루프 중 바뀐 매핑은 다시 승인받으며, 코드 수정은 같은 해시의 패치 산출물로 남긴다.

## 관련 요구사항
AV-017, AV-019, AV-020

## 선행 작업
Blocked by: 14B

## 작업 범위
- 포함: `publish_final_report`, 변경 행 재승인(08B 게이트 재사용, 이전 분석 버전과 행 ID 비교), 통과한 테스트에 대응하는 매핑 행의 `SANDBOX_VERIFIED` 갱신, 수정 패치 산출물(패치·브랜치 이름·PR 제목과 본문·SHA-256 저장)과 소스 해시 대조, `DraftPrPublisher` 인터페이스와 기본 구현(전송 없이 "패치 준비됨" 기록)
- 제외: GitHub App 권한 발급, 실제 push·PR 생성(결정: GitHub App은 쓰지 않는다. 패치를 받은 사람이 직접 적용한다)

## 수용 기준
- [x] 통과 시 발행된 최종 요구사항·소스 참조가 마지막으로 검증한 것과 같다.
- [x] 루프 중 매핑 처리가 바뀐 행이 있으면 발행 전에 그 행만 다시 승인받는다.
- [x] 통과한 테스트에 대응하는 매핑 행의 검증 상태가 `SANDBOX_VERIFIED`로 올라간다.
- [x] 패치를 적용해 만든 소스 해시가 통과한 소스 해시와 같다.
- [x] 패치 산출물에는 자격 증명이 들어가지 않고, 외부로는 아무것도 전송되지 않는다.

## 테스트 전략
- 제안 Test Seam: 발행 Activity와 패치 생성·해시 대조(가짜 Publisher). 잡는 것: 발행 일관성·재승인 범위. 못 잡는 것: 실제 GitHub 동작.
- 실행: `uv run pytest tests/sandbox/test_publish.py`

## 완료 시 확인할 동작
통과한 실행에서 최종 분석 결과와 수정 패치 산출물 참조가 남는다.

## 구현 요약
- **발행 흐름:** 루프가 `PASSED`로 끝나고 정리까지 마친 뒤 `sbx_plan_publish`(바뀐 행 계산) → 바뀐 행이 있으면 `ReapprovalGate` 대기 → `sbx_publish`. 발행은 마지막 검증에 쓴 요구사항·소스 그대로이며, 빌드 때 기록한 소스 트리 해시와 지금 해시가 다르면 `SOURCE_CHANGED_AFTER_VERIFICATION`으로 거부한다.
- **최종 분석 버전(`cmp_worker/final_report.py`):** 이전 분석 버전의 행에 Fixer가 제안한 `RowChange`(MIGRATE/KEEP만, grade·required는 못 바꿈)를 적용하고, 첫 검토의 `required`로 처리를 다시 계산한다. 통과한 테스트가 `covers`로 선언한 기능의 행 중 근거가 확인된(`EVIDENCE_CHECKED`) 행만 `SANDBOX_VERIFIED`로 올린다. 새 버전 ID는 (이전 버전, 최종 요구사항 SHA-256, 행) 해시라 같은 내용이면 같은 참조다.
- **변경 행 재승인:** 처리(disposition, treatment, required)가 이전 승인본과 다른 행만 `ReapprovalGate`(08B `ReviewGate`의 하위 클래스, 같은 Signal 계약·거절 기록)가 다시 묻는다. 바뀐 행은 처리 결과와 상관없이 모두 답이 필요하고, 거절하면 필수 기능이 되어 `BLOCK`일 수 있으며 그러면 `NEEDS_REVIEW`로 끝나 아무것도 발행하지 않는다. Signal은 `approve_changed_rows`, 상태는 Query `reapproval_state`다.
- **패치 산출물(`cmp_worker/fix_patch.py`):** 분석한 소스와 통과한 소스 사이의 `git diff --binary` 패치를 만들고, 임시 복사본에 `git apply`한 결과의 트리 해시가 통과한 소스의 해시와 같을 때만 낸다(다르면 `PATCH_DOES_NOT_REPRODUCE_RESULT`). 패치·브랜치 이름·PR 제목과 본문(마스킹)·해시를 저장소에 참조로 남기고 `DraftPrPublisher`(기본 `PreparedOnlyPublisher`)는 "패치 준비됨"만 기록한다. 외부로는 아무것도 보내지 않는다.

## 결정 (이전 "결정 대기")
- 패치를 받아 PR을 여는 사람과 절차는 이 기능이 정하지 않는다. 산출물은 `published.patch`(브랜치 이름, 패치 참조, 결과 트리 해시)로 호출부에 전달된다.
- "매핑 처리가 바뀐 행"은 `disposition`·`treatment`·`required` 중 하나라도 이전 승인본과 다른 행으로 정의했다. 현재 Fixer는 `disposition`만 바꿀 수 있다.

## 검증 증거
- 단위: `uv run pytest tests/sandbox/test_publish.py`(최종 보고서 규칙, 재승인 게이트, 실제 git 패치), `tests/sandbox/test_publish_activities.py`(발행 Activity와 Fixer 행 변경), `tests/sandbox/test_fix_loop.py`(루프 이력: 발행 순서, 재승인 대기·승인·거절, 실패 시 미발행).
- 실제 k3d: `-k wrong_port`가 발행까지 통과한다. 최종 분석 버전의 해당 행이 `SANDBOX_VERIFIED`이고 발행된 요구사항은 마지막으로 검증한 것과 같다.

## 한계와 미검증
- 재승인 경로와 패치 경로는 가짜 Activity와 단위 테스트로 확인했다. 실제 클러스터에서 코드 수정이 일어나는 경로는 결함 데모 앱이 없어 미검증이다.
- 재승인을 받는 API 경로와 화면(Sandbox 자식 Signal·Query 연결)은 만들지 않았다. 민서님이 API를 맡으므로 `approve_changed_rows` Signal과 `reapproval_state` Query를 알려야 한다.
- `SANDBOX_VERIFIED`는 테스트가 `covers`로 선언한 기능에만 붙는다. 표준 테스트가 어떤 기능을 덮는지 `covers`를 채우는 일은 카탈로그 쪽 후속이다.
- 부모 Workflow는 아직 이 결과(`published`)를 Planning 입력으로 넘기지 않는다(티켓 15).

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-05: 기존 티켓 14에서 분리. "Draft PR"을 "패치 산출물"로 바꿈(GitHub App 미사용).
- 2026-11-05: 구현. 재승인 대상은 처리가 바뀐 행 전체로 정의, 패치는 실제 git으로 만들고 적용 결과 해시를 검증.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
