# Ticket 06C — 앱의 AWS 리소스 사용 추적(선택·비선택 후보 대조, CloudTrail 근거)

## 상태
DONE — PR #248 병합, Issue #247 (2026-11-06). 미확인: 실제 계정·클러스터 후보로 IAM·CloudTrail 근거 확인 1회(승인 필요).

## 목적
코드 스캔만으로는 놓치는 AWS 리소스 사용을 IAM·CloudTrail과 범위 선택의 후보로 보강한다. 지금은 스캐너가 읽은 IAM 정책 결과(`role_policies`)와 CloudTrail 요약(`cloudtrail`)이 `aws-scan` 산출물에 저장만 되고 분석·매핑·승인 어디에도 쓰이지 않는다(`Evidence.kind`의 `IAM_POLICY`·`CLOUDTRAIL`을 만드는 코드도 없다). 또 06B는 선택한 후보만 전체를 넘기고 선택하지 않은 후보는 ID만 넘긴다. 사용자가 본 후보(범위 선택)와 분석이 독립적으로 읽은 값(코드·CloudTrail)을 **대조**해서 어긋나는 지점을 드러낸다.

## 관련 요구사항
AV-003, AV-005, AV-007, AV-008, AV-017, AV-020

## 선행 작업
Blocked by: 06, 06B (병합). 범위 선택 API(티켓 56)는 바꾸지 않는다: 합치는 코드는 06B에서 우리가 만든 `load_scope`다.

## 읽는 값의 분담 (겹치지 않게)
| 값 | 읽는 곳 |
| --- | --- |
| 쿠버네티스 참조(Secret·ConfigMap 이름), Ingress, 서비스 어카운트, 후보 구성 | 범위 선택 API (이미 저장됨) |
| IAM 정책의 리소스 ARN과 액션 | 범위 선택 API 후보의 `IAM_POLICY` 근거 (이미 저장됨). 스캐너의 IAM 읽기는 **그대로 두고 개선하지 않는다**: 와일드카드 여부 확인용 |
| CloudTrail 호출 요약 | **스캐너만** (API는 읽지 않음) |
| 보안 그룹·서브넷·라우트·리스너·대상 그룹 | 스캐너(변경 없음) |

## 작업 범위
- 포함
  - **선택·비선택 후보 전체 전달(06B 후속):** `ConfirmedScope`를 `candidates[]`(후보마다 `selected: bool`)로 바꿔 모든 후보를 같은 구조로 넘긴다. `load_scope`는 분석 기록에 있는 후보 전체를 담는다. 전달되는 값은 후보 필드(이름, ARN, 종류, 근거 문장)뿐이고 비밀 값은 없다(AV-020).
  - **CloudTrail 근거:** 스캐너가 읽은 호출 요약(서비스·이벤트·횟수)을 `AnalysisReport`에 선택 필드로 싣고 `Evidence(kind="CLOUDTRAIL")`를 만든다. 읽지 못한 경우(권한 거부, 조회 없음)는 미수집과 사유를 남긴다.
  - **IAM 와일드카드 표시:** 스캐너가 읽은 `wildcard_resources`를 보고서에 싣는다(구체 리소스를 알 수 없다는 신호). 새 IAM 호출은 만들지 않는다.
  - **대조 규칙(순수 함수 `reconcile_usage`):** 후보(`selected` 여부) × 코드 스캔 호출 지점(`CallSite`) × CloudTrail 호출을 비교해 아래를 `unverified_risks`에 올린다.
    - `SCOPE_EXCLUDED_BUT_USED`: 사용자가 제외한 리소스를 코드나 CloudTrail이 쓴다.
    - `SCOPE_SELECTED_NO_USAGE`: 선택했는데 코드·CloudTrail 모두 사용 흔적이 없다(정보 표시, 차단 아님).
    - `USAGE_NOT_IN_SCOPE`: **코드**에는 있는데 후보에 없다(놓친 의존성 후보). CloudTrail만 본 서비스는 이 경고를 내지 않는다: `LookupEvents`는 계정 전체(다른 주체, 플랫폼 자신의 호출)를 보여 주므로 앱의 사용으로 볼 수 없다.
  - 근거 존재 검사(`check_evidence`): 조사 결과 `CLOUD_RESOURCE`·`IAM_POLICY`·`CLOUDTRAIL`은 `cloud_resource_ids`에 위치가 있어야 확인되는데 파이프라인이 이 값을 비워 두고 있었다. 스캔에서 만든 위치(IAM 역할은 역할 ARN, CloudTrail은 `<이벤트 소스>:<이벤트 이름>`)를 `cloud_resource_ids`로 넘긴다.
- 제외: Mapper가 IAM·CloudTrail 근거를 인용하도록 도구를 주는 일(후속, `agents/tools`·프롬프트 변경 필요), 보안 "권한"·"감사 로그" 기준의 판정(보안 티켓), 스캐너 IAM 읽기의 리소스 ARN 보존(범위 선택 API 후보로 충분), 쿠버네티스 실제 상태와 저장소 매니페스트의 대조, Planning 입력 변경(필요 없음), 새 AWS 호출, 매핑 등급·처리(`APPROVAL`/`BLOCK`) 자동 변경(경고만).

## 수용 기준
- [x] 06B에서 넘기던 선택 후보뿐 아니라 선택하지 않은 후보도 이름·ARN·근거와 함께 분석 입력에 전달되고 `selected`로 구분된다.
- [x] CloudTrail 요약이 보고서에 실리고 `Evidence(CLOUDTRAIL)`로 만들어진다. 근거 존재 검사가 이 종류를 처리한다.
- [x] 제외된 리소스를 코드나 CloudTrail이 쓰면 `SCOPE_EXCLUDED_BUT_USED`가 남는다. 사용 흔적 없는 선택 후보는 `SCOPE_SELECTED_NO_USAGE`, 후보에 없는 **코드** 사용은 `USAGE_NOT_IN_SCOPE`로 남는다. CloudTrail만 본 서비스는 후보 밖 경고를 내지 않는다.
- [x] "호출 기록 없음"은 "쓰지 않음"의 증거가 아니다: CloudTrail은 데이터 이벤트와 오래된 기록이 보이지 않는다는 한계가 경고 문구(코드·사유)에 드러난다.
- [x] 읽지 못한 경우(다른 계정 역할, 권한 거부, IRSA 없음, 와일드카드)는 사유와 함께 남고 "문제 없음"으로 표시되지 않는다.
- [x] 같은 입력이면 같은 결과가 나온다. 비밀 값이 입력·보고서·로그에 새로 들어가지 않는다.
- [x] `scope`·CloudTrail 근거가 없는 기존 입력·보고서는 그대로 읽히고 기존 테스트가 통과한다.

## 테스트 전략
- 제안 Test Seam: ① 순수 함수 `reconcile_usage(scope, call_sites, cloudtrail)`: 후보 선택 여부 × 사용 흔적 표(세 가지 경고와 아무 경고 없음). ② `load_scope`와 시작 요청(06B의 `test_scope_start_input.py` 방식): 선택·비선택 후보 모두 전달. ③ 고정 CloudTrail 응답(`botocore.stub.Stubber`)에서 근거 생성. 잡는 것: 대조 규칙, 근거 형식, 하위 호환. 못 잡는 것: 실제 계정의 CloudTrail 모양과 범위 선택 후보(실제 1회 확인은 06과 함께, 승인 필요).
- 실행: `uv run pytest tests/test_scope_start_input.py tests/analysis -q`, 이후 `make check`

## 문서
spec AV-005(IAM·CloudTrail을 근거로 쓰는 방식과 한계), AV-017(`unverified_risks` 코드), tasks.md를 같은 PR에서 갱신한다. `ConfirmedScope` 구조 변경은 06B 문서를 함께 고친다. 계약 변경은 병합 직후라 실행된 기록이 없어 호환 문제가 작다.

## 확인한 것과 한계 (2026-11-06)
- CloudTrail `LookupEvents`는 관리 이벤트만 보고 계정 전체를 포함한다. 앱의 데이터 호출(S3 객체, SQS 메시지)은 보이지 않을 수 있어 "호출 기록 없음"은 근거가 약하다(경고 문구 `CLOUDTRAIL_DATA_EVENTS_NOT_VISIBLE`). 실제 계정에서 얼마나 쓸모 있는지는 06의 실제 계정 실행에서 확인한다.
- 스캐너의 IAM 읽기는 유지했고 바꾸지 않았다(결정: 유지). 읽은 역할·와일드카드 여부를 보고서에 싣는다.

## 결정 대기
- Mapper가 IAM·CloudTrail 근거를 인용하게 하는 도구(후속 티켓).
- 경고 코드(`SCOPE_*`, `USAGE_NOT_IN_SCOPE`)를 승인 화면에 어떻게 보일지(상헌님).

## 관련 문서
- [06 AWS 스캔](06-aws-infra-scan.md) · [06B 범위 입력](06b-scope-input.md) · [요구사항](../../specs/analysis-verification/spec.md)
