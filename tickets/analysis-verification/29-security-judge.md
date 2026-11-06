# Ticket 29 — 보안 동등 판정과 약점 표시, 승인 게이트 연결

## 상태
DRAFT — 사용자 검토 전. Issue 미생성.

## 목적
측정(28)을 원본 대비 대상(예정) 상태와 비교해 `LOWER`/`SAME`/`HIGHER`/`UNKNOWN`을 내고, 낮아짐과 원본 약점을 기존 필수 기능·손실 승인 게이트(AV-022)에 올린다.

## 관련 요구사항
AV-023, AV-022

## 선행 작업
Blocked by: 28, 08B. **결정 필요:** "원본 약점을 승인하면 대상에서 고쳐서 배포"는 제품 정책 변경이다(`docs/product/policies.md`). 정책이 정해지기 전에는 판정·표시·승인 기록까지만 하고 보강 적용은 하지 않는다.

## 작업 범위
- 포함
  - 대상(예정) 상태 산출: 첫 사이클은 공통 렌더 v2의 입력 기준(예: 대상 진입 프로토콜, 생성되는 NetworkPolicy)에서 읽는다. 입력에 없으면 `UNKNOWN`.
  - 순수 함수 `judge_security(source_measures, target_plan)`: 기준별 판정과 `WEAK_SOURCE`.
  - 처리 매핑: `LOWER`는 `APPROVAL`(잃는 것 표시) 또는 `BLOCK`, `UNKNOWN`은 사람 검토 항목. 승인 기록은 `ReviewRecord`에 같은 방식으로 남긴다.
  - 약점 보강 제안(문구와 대상 구성 변화 요약)을 분석 결과에 싣는다. 적용은 하지 않는다.
- 제외: 보강의 대상 구성 반영(Provisioning 계약 합의 필요), 비교 화면(상헌), 배포 후 측정.

## 수용 기준
- [ ] 원본 HTTPS, 대상 HTTP 입력은 `LOWER`이고 승인 없이 Sandbox로 가지 않는다.
- [ ] 원본 평문 DB 연결은 `WEAK_SOURCE`로 표시되고 보강 제안이 붙는다.
- [ ] 승인/거절이 기록되고 같은 분석 버전에서 재사용된다. 자동 승인은 없다.
- [ ] 같은 입력이면 같은 판정이 나온다.
- [ ] Workflow 변경이 있으면 replay 호환성을 검토한다(기존 Signal·Query는 유지).

## 테스트 전략
- 제안 Test Seam: `judge_security` 순수 함수(기준×원본/대상 조합 표)와 08B 승인 게이트의 기존 테스트 확장. 잡는 것: 판정 표, 처리 매핑. 못 잡는 것: 실제 대상 환경의 상태(배포 후 측정, 타 담당).
- 실행: `uv run pytest tests/analysis/test_security_judge.py tests/analysis -q`

## 문서
spec AV-023, plan, 승인 신호 설명(runbook §5)을 같은 PR에서 갱신한다. 정책 문서 변경은 사용자 결정 후.

## 결정 대기
- 약점 보강 승인을 제품 정책으로 인정할지(사용자)
- 대상(예정) 상태를 읽는 입력 필드(민서와 확인)
