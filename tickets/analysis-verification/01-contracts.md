# Ticket 01 — 분석·검증 계약 모델

## 상태
DONE — 사용자 최종 확인, PR #31 main 병합, Issue #29

## 목적
분석 결과·Sandbox 검증 결과의 계약을 정의하고, 요구사항과 Planning 투영은 main에 구현된 v2 모델(`RequirementsV2`, `PlanningRequestV2`)을 재사용한다.

## 관련 요구사항
AV-001, AV-017, AV-018, AV-020, AV-022

## 선행 작업
Blocked by: 없음

## 작업 범위
- 포함: `cmp_contracts.analysis`(AnalysisRequest, Evidence, MappingRow(`required_proposed`는 매핑 시 항상 채움, `required`·`treatment`는 사용자 확정 전 null 허용), ReviewRecord, UsageProfile, PrecheckReport, ExternalEntry, EnvExceptions, AnalysisReport)와 `cmp_contracts.verification`(SourceArchive, BuildAttempt, SandboxAttempt, Verdict, SandboxVerification, `PlanningRequestV2` 투영 함수), 요구사항 JSON 정규 직렬화(같은 내용 → 같은 바이트·SHA-256), fixture
- 제외: Provisioning 모델(`provisioning`, `provisioning_v2`, `shared_deployment*`) 수정, 스캐너·Activity 구현, `*_ref` 저장 위치(D-02)

## 수용 기준
- [x] 엄격한 모델(`extra='forbid'`, 암묵적 변환 금지)이 알 수 없는 필드와 잘못된 Enum을 거절한다. Enum 값은 대문자다.
- [x] 분석 결과 모델 이름이 `AnalysisReport`이고 Provisioning v1 `AnalysisResult`와 겹치지 않는다.
- [x] 요구사항을 새로 정의하지 않고 `RequirementsV2`로 만들며, fixture 요구사항 JSON이 `RequirementsV2` 검증을 통과한다.
- [x] 같은 요구사항을 두 번 직렬화하면 같은 바이트·같은 SHA-256이 나온다.
- [x] 모델 어디에도 비밀 값을 담는 필드가 없다. 비밀은 변수 이름이나 Secret 참조로만 나타낸다. `EnvExceptions.replaced`에 넣을 변수가 비밀이 아닌지 판단하는 것은 분석(05·08)의 몫이고, 01은 형식만 검증한다.
- [x] fixture(EKS → K3s)가 검증을 통과하고 JSON 왕복 후 같다.
- [x] fixture를 `PlanningRequestV2`로 투영한 결과가 `prepare_plan_v2`(requirements_bytes 포함) 검증을 통과한다.
- [x] `AnalysisReport`·`EnvExceptions` 형식과 외부 진입 처리(D-15)를 민서님께 공유할 요약을 PR 설명에 정리한다. 전달은 사용자가 한다(사용자 확인).

## 테스트 전략
- 합의된 Test Seam: 공개 모델의 `model_validate`/`model_dump`와 `prepare_plan_v2`를 통한 투영 검증. 잡는 것: 형식·필수값·정의되지 않은 비밀 값 필드 유입·v2 대조 규칙. 못 잡는 것: 허용된 문자열의 비밀 여부, 실제 저장소 조회와 상대 Worker 실행.
- 실행: `uv run pytest tests/analysis/test_contracts.py`, `make check`

## 완료 시 확인할 동작
fixture 하나로 분석 결과·요구사항·검증 결과를 만들고, 투영 결과가 민서님 Planning v2 검증을 통과한다.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 계약 협의안 v2(리소스 목록·validated_workloads) 반영, AnalysisReport로 이름 변경, 완료 조건을 '공유까지'로 축소.
- 2026-11-04(v2 동기화): main의 v2 구현(#22, #24, B-01 변경) 기준으로 수정.
- 2026-11-04(목적 점검): 필수 여부 제안·확정 필드와 ReviewRecord 추가(AV-022).
- 2026-11-04(착수 전 검토): 제목에서 v1 제거, AV-022 연결, 비밀 기준을 형식 검증으로 한정, 공유는 요약 준비까지로 조정, v2 계약 링크로 교체.
- 2026-11-04(구현): Sandbox 내부 기록에 검증한 분석·대상 선택 참조를 추가하고 투영 전 대조한다(AV-018). 요구사항·설정 참조는 분석과 Sandbox 증거를 각각 보존한다. 상세 모델과 직렬화는 plan §3에 기록한다.
- 2026-11-04(사용자 리뷰): plan §2.3·§3의 매핑 행에 분석 버전별 고유 ID를 추가하고 필수 여부·승인 키를 행 ID로 변경했다. `project_planning`은 검증된 모델을 받고, 실제 값이 든 fixture와 유효성 검증을 거친 실패 사례로 테스트한다.
- 2026-11-04(INFRA·D-07 반영): Sandbox 시도 실패 분류에 `INFRA`(환경 실패) 추가.

## 검증 증거
- `uv run --extra dev python -m pytest tests/analysis/test_contracts.py tests/test_provisioning_v2.py -q`: 31 통과.
- `uv run --extra dev python -m ruff check .`, `uv run --extra dev python -m ruff format --check .`: 통과.
- Windows 전체 pytest: 221 통과, 49 실패. Helm이 없고 Preview 테스트의 POSIX 셸 fixture가 Windows에서 실행되지 않았다.
- Ubuntu 24.04.5/WSL, Python 3.11.18, Helm 3.16.4: PR 병합 전 최종 코드에서 `make check` 통과(276 passed, 경고 1건). 잠금 파일의 의존성과 검증한 Helm을 임시 환경에서 사용했다.
- 실제 저장소·Worker 실행과 허용된 문자열의 비밀 여부는 검증 범위 밖이다. `ReviewRecord`가 가리키는 분석 버전에서 행 ID가 실제 존재하는지와 승인 흐름은 08B에서 검증한다. fixture 기반 검사는 실제 Sandbox 실행을 증명하지 않는다.

## 민서님 공유용 요약
- `AnalysisReport`는 분석 버전·사용 프로필·매핑·사전 점검·원본 DB 이미지·외부 진입·미검증 항목을 보관한다. 매핑 행 ID는 같은 분석 버전 안에서 고유하며 검토·승인 기록의 키로 사용한다.
- `EnvExceptions`는 제외·누락 변수 이름과 비밀이 아닌 대체 값만 보관한다. 대체 값의 비밀 여부는 분석 측에서 판단한다.
- 인터넷에서 앱으로 들어오는 진입은 v2 `network_intent`와 별도로 `ExternalEntry`에 기록한다(D-15).
- Sandbox 내부 결과에 분석·대상 선택 버전을 보관해 인계 시 대조하며, 기존 Provisioning v2 모델은 변경하지 않는다.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 사용자 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
