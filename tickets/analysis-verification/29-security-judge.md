# Ticket 29 — 보안 동등 판정과 약점 표시, 승인 게이트 연결

## 상태
IN_PROGRESS — **29-A**(판정과 표시)는 구현·자동 테스트 완료(2026-11-06). **29-B**(승인 게이트 연결)는 DRAFT: 정책 결정(D-17)과 상헌님 합의 후. 29-A·29-B는 한 Issue로 다룬다. Issue 번호는 사용자가 발행한 뒤 기록한다.

## 목적
측정(28)을 대상(예정) 상태와 비교해 `LOWER` / `SAME` / `HIGHER` / `UNKNOWN`을 내고, 원본부터 약한 항목(`WEAK_SOURCE`)과 보강 제안을 분석 결과에 싣는다. 이후 낮아짐을 기존 필수 기능·손실 승인 게이트(AV-022)에 올려 승인 없이 Sandbox로 가지 못하게 한다.

## 관련 요구사항
AV-023, AV-022

## 선행 작업
Blocked by: 28, 08B (병합됨). 29-B는 D-17(약점 보강을 제품 정책으로 받을지)과 상헌님(승인 화면·API) 합의가 필요하다.

## 29-A — 판정과 표시 (구현 완료, Workflow 변경 없음)
- **대상(예정) 상태** (`cmp_scanners/security_judge.py`의 `target_security`): 대상 Provider와 요구사항에서 기준별로 계산한다. 값은 플랫폼이 대상에 만드는 것을 코드 데이터로 적은 것이라 대상 쪽이 바뀌면(예: 입구 TLS) 한 줄만 바꾸면 된다. 정의가 없으면 `UNKNOWN`. Provisioning·트래픽 담당 확인이 필요한 값이다.

  | 기준 | 대상 상태 | 근거 |
  | --- | --- | --- |
  | 외부 HTTPS | ON_PREM `UNPROTECTED`(`TARGET_ENTRY_WITHOUT_TLS`), AWS·GCP `UNKNOWN` | ADR-006 §4: P0 대상에는 도메인·TLS가 없다. 클라우드 입구는 아직 정의 없음 |
  | DB 접근 범위 | 요구사항에 DB가 있으면 `PROTECTED`(`TARGET_DEFAULT_DENY_NETWORK_POLICY`) | 대상 NetworkPolicy는 `network_intent`로 만들어지고 기본 차단에서 시작(티켓 26) |
  | DB 전송 암호화 | `UNPROTECTED`(`TARGET_DB_SSL_NOT_CONFIGURED`) | DB 차트에 SSL 설정이 없고 플랫폼이 만드는 연결에 `sslmode`가 없다 |
  | 운영용 API | 원본이 `NOT_APPLICABLE`/`UNKNOWN`이 아니면 `UNPROTECTED`(`TARGET_ENTRY_HAS_NO_PATH_FILTER`) | 대상 입구는 경로를 거르지 않는다 |
  | (Provider 없음) | 전부 `UNKNOWN`(`TARGET_PROVIDER_UNKNOWN`) | |
- **판정** (`judge_security`): 상태 강도는 `PROTECTED` > `PARTIAL` > `UNPROTECTED`. 대상이 원본보다 낮으면 `LOWER`, 같으면 `SAME`, 높으면 `HIGHER`. 어느 쪽이든 `UNKNOWN`이면 `UNKNOWN`(같은 수준으로 보지 않는다). `NOT_APPLICABLE`이 한쪽에 있으면 `SAME`. 기준 순서는 고정이고 같은 입력이면 같은 결과다.
- **약점·제안:** 원본이 `UNPROTECTED`·`PARTIAL`이면 `weak_source`, 약점이거나 `LOWER`이면 제안 코드(`ENFORCE_HTTPS_AT_ENTRY`, `RESTRICT_DB_INGRESS_TO_APP`, `REQUIRE_DB_SSL`, `BLOCK_ADMIN_PATHS_AT_ENTRY`). 문구는 화면이 고르고 **적용하지 않는다**.
- **보고서:** `AnalysisReport.security_judgements`(선택 필드)와 `unverified_risks`의 `SECURITY_LOWER: <기준>`·`SECURITY_WEAK_SOURCE: <기준>`. 입력의 대상 Provider(티켓 31의 `target_provider`)와 요구사항을 쓴다.
- 데모 원본(CloudFront HTTPS, DB 접근 무제한, 평문 DB, 운영 경로 노출)을 ON_PREM 대상에 판정하면: 외부 HTTPS `LOWER`, DB 접근 `HIGHER`, DB 전송 암호화 `SAME`(약점), 운영용 API `SAME`(약점)이다. 기획의 "대상 HTTPS로 같음"과 "SSL 강제·운영 경로 차단으로 높아짐"은 29-B와 보강 반영(Provisioning)이 있어야 한다.

## 29-B — 승인 게이트 연결 (DRAFT)
- `LOWER`를 `APPROVAL`(잃는 것 표시)로 올려 승인 없이 Sandbox로 가지 못하게 한다. 승인·거절은 `ReviewRecord`에 같은 방식으로 남기고 같은 분석 버전에서 재사용한다. 자동 승인은 없다. `UNKNOWN`은 사람 검토 항목.
- 지금 `ReviewGate`는 매핑 행 ID 단위(`approvals: {행ID: ...}`)다. 보안 항목은 키 `security:<기준>`을 허용하는 확장이 필요하다. 분석 자식 Workflow 안의 상태 기계라 **`workflow.patched`로 replay 호환을 검토**한다(기존 Signal·Query 유지).
- 승인 API·화면은 상헌님과 합의한다(지금은 CLI Signal만).

## 작업 범위
- 29-A 포함: 위 대상 상태, 판정, 약점·제안, 보고서, 테스트.
- 제외: 보강의 대상 구성 반영(Provisioning 계약 합의 필요), 비교 화면(상헌), 배포 후 측정, Sandbox 프로브(30), 29-B(별도 결정 후).

## 수용 기준 (29-A)
- [x] 원본 HTTPS, 대상 HTTP(ON_PREM, TLS 없음) 입력은 `LOWER`로 판정된다. 대상에 TLS가 있는 값이면 `SAME`이다(프로필 값만 바꾸면 됨).
- [x] 원본 평문 DB 연결은 `weak_source`로 표시되고 제안 코드(`REQUIRE_DB_SSL`)가 붙는다.
- [x] 어느 쪽이든 `UNKNOWN`이면 판정은 `UNKNOWN`이다. Provider를 모르면 전부 `UNKNOWN`이다.
- [x] 같은 입력이면 같은 판정과 같은 순서가 나온다. 제안은 코드일 뿐 적용되지 않는다.
- [x] 보고서에 판정과 위험 코드가 실리고, 필드가 없는 기존 보고서가 그대로 읽힌다. Workflow 변경이 없다.

## 수용 기준 (29-B, 결정 후)
- [ ] `LOWER`는 승인 없이 Sandbox로 가지 못한다. 승인·거절이 기록되고 같은 분석 버전에서 재사용된다. 자동 승인이 없다.
- [ ] Workflow 변경은 replay 호환을 검증한다. 승인 API·화면 연결은 상헌님과 합의한다.

## 테스트 전략
- Test Seam: 순수 함수 `judge_security`(원본 × 대상 표)와 `target_security`(Provider·요구사항), 파이프라인 보고서. 잡는 것: 판정 표, `UNKNOWN` 처리, 제안 조건, 기준 순서. 못 잡는 것: 대상 능력 프로필이 Provisioning의 실제 동작과 같은지(담당 확인).
- 실행: `uv run pytest tests/analysis/test_security_judge.py -q`, 이후 `make check`

## 문서
spec AV-023, plan, tasks.md를 같은 PR에서 갱신한다. 정책 문서(`policies.md`) 변경은 D-17 결정 후.

## 결정 대기
- D-17: 약점 보강을 제품 정책으로 받을지(`policies.md` 변경), 보강의 대상 구성 반영 방법(Provisioning 계약).
- 대상 능력 프로필 값(입구 TLS 없음, DB SSL 없음, 경로 필터 없음)이 Provisioning·트래픽의 실제 동작과 맞는지 확인(담당자). 특히 외부 HTTPS는 ADR-006(P0에 도메인·TLS 없음)을 그대로 옮긴 값이며, 기획의 데모 1행("대상 입구 HTTPS")과 어긋난다. ADR-006은 트래픽 담당 영역이라 이 티켓에서 바꾸지 않는다.
- 29-B의 승인 신호 형식(`security:<기준>` 키)과 승인 화면.
