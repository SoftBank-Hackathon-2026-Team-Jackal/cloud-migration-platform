# Ticket 29 — 보안 동등 판정과 약점 표시, 승인 게이트 연결

## 상태
DONE — PR #257·#261 병합, Issue #256. 29-A(판정과 표시)와 29-B(승인 게이트 연결) 구현 완료(2026-11-06). 남음: 상헌님과 승인 화면·API 합의, 정책 P-12 사용자 검토

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
  | 외부 HTTPS | ON_PREM `PROTECTED`(`TARGET_ENTRY_TLS_AT_RELAY`; 값은 `ON_PREM_ENTRY_TLS`), AWS·GCP `UNKNOWN` | 중계 서버가 신뢰되는 인증서로 HTTPS를 받는다(2026-11-07, `infra/relay/setup-tls.sh`). 클라우드 입구는 아직 정의 없음 |
  | DB 접근 범위 | 요구사항에 DB가 있으면 `PROTECTED`(`TARGET_DEFAULT_DENY_NETWORK_POLICY`) | 대상 NetworkPolicy는 `network_intent`로 만들어지고 기본 차단에서 시작(티켓 26) |
  | DB 전송 암호화 | `UNPROTECTED`(`TARGET_DB_SSL_NOT_CONFIGURED`) | DB 차트에 SSL 설정이 없고 플랫폼이 만드는 연결에 `sslmode`가 없다 |
  | 운영용 API | 원본이 `NOT_APPLICABLE`/`UNKNOWN`이 아니면 `UNPROTECTED`(`TARGET_ENTRY_HAS_NO_PATH_FILTER`) | 대상 입구는 경로를 거르지 않는다 |
  | (Provider 없음) | 전부 `UNKNOWN`(`TARGET_PROVIDER_UNKNOWN`) | |

  **대상에 입구 TLS가 있을 때의 값(합의, 2026-11-07 — 중계 서버 HTTPS 적용으로 `TRUSTED`가 기본값이 됐다):** 중계 서버가 HTTPS로 받으면 사용자 입구가 암호화되므로 CloudFront 입구와 비슷한 모양이다. 다만 **브라우저가 신뢰하는 인증서**(도메인과 정식 인증서)면 `PROTECTED`, **자체 서명처럼 브라우저 경고가 뜨는 인증서**면 암호화는 되지만 원본의 정식 HTTPS와 같다고 보지 않아 `PARTIAL`(사유 `TARGET_TLS_UNTRUSTED_CERT`)로 적는다. TLS가 없으면 지금처럼 `UNPROTECTED`다. 이 값은 한 곳(`cmp_scanners.security_judge.ON_PREM_ENTRY_TLS`: `TRUSTED`·`UNTRUSTED`·`NONE`)만 바꾸면 되고 판정 코드는 그대로다. 오리진 구간과 인터넷에서 ALB로의 직접 접근은 보지 않는다(티켓 28 알려진 한계).
- **판정** (`judge_security`): 상태 강도는 `PROTECTED` > `PARTIAL` > `UNPROTECTED`. 대상이 원본보다 낮으면 `LOWER`, 같으면 `SAME`, 높으면 `HIGHER`. 어느 쪽이든 `UNKNOWN`이면 `UNKNOWN`(같은 수준으로 보지 않는다). `NOT_APPLICABLE`이 한쪽에 있으면 `SAME`. 기준 순서는 고정이고 같은 입력이면 같은 결과다.
- **약점·제안:** 원본이 `UNPROTECTED`·`PARTIAL`이면 `weak_source`, 약점이거나 `LOWER`이면 제안 코드(`ENFORCE_HTTPS_AT_ENTRY`, `RESTRICT_DB_INGRESS_TO_APP`, `REQUIRE_DB_SSL`, `BLOCK_ADMIN_PATHS_AT_ENTRY`). 문구는 화면이 고르고 **적용하지 않는다**.
- **보고서:** `AnalysisReport.security_judgements`(선택 필드)와 `unverified_risks`의 `SECURITY_LOWER: <기준>`·`SECURITY_WEAK_SOURCE: <기준>`. 입력의 대상 Provider(티켓 31의 `target_provider`)와 요구사항을 쓴다.
- 데모 원본(CloudFront HTTPS, DB 접근 무제한, 평문 DB, 운영 경로 노출)을 ON_PREM 대상에 판정하면(중계 서버 HTTPS 적용 뒤): 외부 HTTPS `SAME`, `LOWER`는 없다. 입구에 TLS가 없던 때(`ON_PREM_ENTRY_TLS=NONE`)는: 외부 HTTPS `LOWER`, DB 접근 `HIGHER`, DB 전송 암호화 `SAME`(약점), 운영용 API `SAME`(약점)이다. 기획의 "대상 HTTPS로 같음"과 "SSL 강제·운영 경로 차단으로 높아짐"은 29-B와 보강 반영(Provisioning)이 있어야 한다.

## 29-B — 승인 게이트 연결 (구현 완료)
결정(2026-11-06): 약점 보강은 표시·승인 기록까지(적용은 후속), 승인 대상은 `LOWER`만, 승인을 거절하면 `BLOCK`. 제품 정책 P-12(보안 수준의 동등성)로 올렸다.
- **게이트:** `ReviewGate(analysis_ref, rows, security=…)`가 판정이 `LOWER`인 항목을 `security:<기준>` 항목으로 승인받는다. 필수 기능 확정 뒤 승인 단계에서 매핑 행의 `APPROVAL`과 함께 묻고, 모든 승인 대상에 답해야 끝난다(빠지면 `MISSING_ROW`, `LOWER`가 아닌 기준은 `NOT_APPROVABLE`, 모르는 ID는 `UNKNOWN_ROW`). 거절하면 필수로 바꿔 재계산하는 단계 없이 바로 `BLOCKED`이고, 사유(`SECURITY_LOWER: <기준>`)와 제안 코드가 `blocked`에 실린다. 보안 항목에는 필수 여부가 없어서 `required`에 들어가지 않는다. 매핑 행이 없어도 `LOWER`가 있으면 게이트가 선다.
- **`UNKNOWN`·약점·`SAME`·`HIGHER`는 승인 대상이 아니다.** 상태 조회(`review_state`)의 `security`에 모든 판정(원본·대상 상태, 판정, 약점, 제안, 증명 방식 `SANDBOX`/`POST_DEPLOY`, `approvable`, 지금까지의 `decision`)이 실려 승인 화면이 나란히 보일 수 있다. `proof`는 클러스터 안의 보호(DB 접근, DB 전송 암호화)는 `SANDBOX`, 외부 네트워크를 지나는 항목(외부 HTTPS, 입구 경로 차단)은 `POST_DEPLOY`다.
- **신호와 CLI는 그대로다.** `approve --approve security:EXTERNAL_HTTPS=APPROVED`처럼 같은 `approve_losses` 신호에 `security:<기준>` 키로 답한다. 승인·거절은 `ReviewRecord.approvals`에 같은 키로 남고 Sandbox 이후 흐름은 이 키를 행 ID로 가정하지 않아 그대로 이어진다.
- **Workflow:** 분석 자식의 `_review`가 `workflow.patched("security-gate-v1")` 아래에서 `check_evidence` 결과의 `security_judgements`를 읽는다. 이 필드가 없는 기존 이력은 이전과 같게 동작한다. `check_evidence`의 반환에 필드가 더해졌을 뿐 Activity 이름·입력은 같다.
- **수정이 보안을 낮추는 것 막기(`cmp_worker/security_guard.py`):** `sbx_fix`가 Fixer의 수정안을 쓰거나 저장하기 전에 확인한다. (1) 코드 수정이 파일에 없던 평문 `sslmode`(`disable`·`allow`·`prefer`)를 들이면(원본이 이미 평문이면 제외) (2) 요구사항 수정으로 판정을 다시 했을 때 대상 상태가 나빠지거나 `LOWER`가 새로 생기면 거절하고 `SECURITY_LOWERED_BY_FIX:<기준>`으로 사람 검토에 넘긴다. 현재 대상 상태는 Provider·DB 유무·원본 운영용 API 측정에서만 정해져서 요구사항 수정이 판정을 바꾸는 경우는 드물다(규칙은 대상 상태가 의도에 의존하게 되면 쓰인다).

## 작업 범위
- 29-A 포함: 위 대상 상태, 판정, 약점·제안, 보고서, 테스트.
- 제외: 보강의 대상 구성 반영(Provisioning 계약 합의 필요), 비교 화면(상헌), 배포 후 측정, Sandbox 프로브(30), 29-B(별도 결정 후).

## 수용 기준 (29-A)
- [x] 원본 HTTPS, 대상 HTTP(ON_PREM, TLS 없음) 입력은 `LOWER`로 판정된다. 대상에 TLS가 있는 값이면 `SAME`이다(프로필 값만 바꾸면 됨).
- [x] 원본 평문 DB 연결은 `weak_source`로 표시되고 제안 코드(`REQUIRE_DB_SSL`)가 붙는다.
- [x] 어느 쪽이든 `UNKNOWN`이면 판정은 `UNKNOWN`이다. Provider를 모르면 전부 `UNKNOWN`이다.
- [x] 같은 입력이면 같은 판정과 같은 순서가 나온다. 제안은 코드일 뿐 적용되지 않는다.
- [x] 보고서에 판정과 위험 코드가 실리고, 필드가 없는 기존 보고서가 그대로 읽힌다. Workflow 변경이 없다.

## 수용 기준 (29-B)
- [x] `LOWER`는 승인 없이 Sandbox로 가지 못한다(분석 자식이 승인을 기다린다). 승인·거절이 `ReviewRecord`에 기록된다. 자동 승인이 없다. 매핑 행이 없어도 `LOWER`가 있으면 선다.
- [x] 거절하면 `BLOCK`되고 사유와 제안 코드가 보고된다. `UNKNOWN`·약점은 표시만 하고 승인을 요구하지 않는다.
- [x] 상태 조회에 모든 보안 판정과 증명 방식이 실리고, 기존 신호 이름·CLI·`ReviewRecord` 흐름은 그대로다.
- [x] Fixer의 수정이 평문 `sslmode`를 들이거나 판정을 나쁘게 하면 거절되고, 코드 수정은 쓰이지 않는다.
- [x] 분석 자식 Workflow 변경은 `workflow.patched`로 기존 이력과 호환된다. 필드가 없는 결과는 이전과 같다.
- [ ] 상헌님과 승인 화면·API 합의, 정책 P-12 검토(사용자).

## 테스트 전략
- Test Seam: 순수 함수 `judge_security`(원본 × 대상 표)와 `target_security`(Provider·요구사항), 파이프라인 보고서. 잡는 것: 판정 표, `UNKNOWN` 처리, 제안 조건, 기준 순서. 못 잡는 것: 대상 능력 프로필이 Provisioning의 실제 동작과 같은지(담당 확인).
- 실행: `uv run pytest tests/analysis/test_security_judge.py -q`, 이후 `make check`

## 문서
spec AV-023, plan, tasks.md를 같은 PR에서 갱신한다. 정책 문서(`policies.md`) 변경은 D-17 결정 후.

## 결정 대기
- D-17: 약점 보강을 제품 정책으로 받을지(`policies.md` 변경), 보강의 대상 구성 반영 방법(Provisioning 계약).
- 대상 능력 프로필 값(입구 TLS 없음, DB SSL 없음, 경로 필터 없음)이 Provisioning·트래픽의 실제 동작과 맞는지 확인(담당자). 특히 외부 HTTPS는 ADR-006(P0에 도메인·TLS 없음)을 그대로 옮긴 값이며, 기획의 데모 1행("대상 입구 HTTPS")과 어긋난다. ADR-006은 트래픽 담당 영역이라 이 티켓에서 바꾸지 않는다.
- 승인 화면이 `review_state`의 `security`를 어떻게 보일지와 승인 API(상헌님). 지금은 CLI Signal뿐이다.
- 정책 P-12를 사용자가 검토(약점 보강의 대상 구성 반영은 Provisioning 계약 뒤).
