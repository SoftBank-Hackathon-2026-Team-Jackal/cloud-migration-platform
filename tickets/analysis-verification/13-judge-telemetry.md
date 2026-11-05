# Ticket 13 — 통신 규칙·판정·실행 기록

## 상태
IN_PROGRESS — 구현 완료(실제 k3d `cmp-sandbox`에서 검증), 리뷰 대기, Issue 번호 미기록

## 목적
`network_intent`로 Sandbox NetworkPolicy와 출구 규칙을 만들고, 객관적 신호로 통과 여부를 정한다.

## 관련 요구사항
AV-013, AV-014

## 선행 작업
Blocked by: 08, 11, 12

## 작업 범위
- 포함: NetworkPolicy 생성(기본 차단, `network_intent`의 리소스 간 항목·외부 진입·DNS 허용, 외부는 출구 프록시만), 프록시 허용 목록(intent의 `external_host` 항목 + `KEEP` 서비스), `MIGRATE` 서비스 원본 주소·목록 밖 호스트 차단·기록, `judge`, 호출 지점별 테스트 대응 표시(`SuiteCase.covers` 선언 기준)
- 제외: 커버리지 계측 이미지, Production 정책 변환(민서)

## 수용 기준
- [x] `network_intent`에 없는 리소스 간 통신은 막힌다.
- [x] intent의 `external_host`와 `KEEP` 서비스(첫 사이클 S3)로의 호출은 허용·기록된다.
- [x] 원본 DB·원본 ALB 같은 `MIGRATE` 원본 주소와 목록 밖 호스트로의 시도는 차단·기록되고 판정이 실패하며 이유에 호스트가 나온다.
- [x] 필수 테스트 하나라도 실패하면 통과하지 않는다.
- [x] 같은 입력이면 같은 판정이 나온다.

## 테스트 전략
- 제안 Test Seam: `judge(...)` 순수 함수 + k3d에서 NetworkPolicy·프록시 기록 수집. 잡는 것: 판정 규칙·외부 호출 감지. 못 잡는 것: 프록시를 우회하는 비HTTP 경로.
- 실행: `uv run pytest tests/sandbox/test_judge.py`, k3d 통합 테스트

## 완료 시 확인할 동작
intent에서 외부 목적지를 지운 fixture가 판정에서 실패하고 호스트가 보고된다.

## 구현 요약
- `cmp_worker/egress_proxy.py`: 표준 라이브러리만 쓰는 허용 목록 프록시(HTTP 전달 + `CONNECT`). 허용/차단 결정마다 JSON 한 줄(`decision`, `host`, `port`, `method`)만 로그로 남기고 헤더·본문·쿼리는 남기지 않는다. 스크립트는 ConfigMap으로 넣고 다이제스트 고정한 `python` 이미지로 실행한다(비특권·읽기 전용 루트).
- `cmp_worker/sandbox_network.py`: 기본 차단(Ingress/Egress) + DNS + `network_intent`의 리소스 간 항목(출발 Egress·도착 Ingress 짝) + 앱 포트로의 진입. 외부 호스트는 직접 규칙 없이 프록시 허용 목록(intent `external_host` + `KEEP` 호스트)에만 들어가고, 앱은 프록시로만 나갈 수 있다. 프록시 자신은 사설 대역을 뺀 외부로만 나갈 수 있어 다른 규칙을 우회하는 통로가 되지 못한다. `prepare_sandbox(network=True)`는 앱 env Secret에 `HTTP(S)_PROXY`·`NO_PROXY`를 넣는다.
- `SandboxService.apply_network`, `egress_records`(프록시 로그 집계), `external_cut`(12번 `cut_dependency`에 끼울 외부 호스트 차단: 프록시 scale 0 → 복구).
- `cmp_worker/sandbox_judge.py`: `judge`는 순수·결정적 함수. 사유는 `NO_TESTS_PASSED`, `TEST_FAILED:<id>`, `EGRESS_BLOCKED:<host>:<port>`(의도 누락), `MIGRATE_SOURCE_CALLED:<host>:<port>`(원본 주소 호출), `UNSUPPORTED_CALL:<패턴>`을 정렬해 돌려준다. `coverage_report`는 `MIGRATE` 기능 중 `covers`를 선언한 테스트가 없는 것을 알려 준다(선언 기준이며 실행 계측이 아님).
- `SuiteCase.covers` 선택 필드 추가(12번 모델, 기존 묶음은 그대로 읽힘).

## 결정 (이전 "결정 대기")
- 출구 프록시는 Sandbox 워커 소유다(plan 2.7: NetworkPolicy·프록시는 Sandbox 워커 생성). Production 정책 변환은 계속 Provisioning(민서) 몫이며, 공유 차트로 합칠지는 대기.
- 허용 목록은 출발 리소스별이 아니라 Sandbox 전체에 하나다.

## 검증 증거
- 단위: `uv run pytest tests/sandbox/test_egress_proxy.py tests/sandbox/test_network.py tests/sandbox/test_judge.py`.
- 실제 k3d: `uv run pytest tests/integration/sandbox -m k3d -k network_rules`. 입구 규칙 아래에서도 `check_sandbox` 통과, 프록시 경유로 허용 호스트 200·미등록 호스트 403, 프록시 없이 직접 외부 호출 실패, 앱→DB(intent)는 연결·DB→앱(intent 없음)은 차단, 프록시 기록의 `ALLOW`/`BLOCK`이 판정 사유(호스트 포함)로 연결됨, `external_cut` 후 복구 확인.

## 한계와 미검증
- 앱이 `HTTP(S)_PROXY`를 무시하고 직접 나가면 NetworkPolicy가 조용히 막을 뿐 기록은 남지 않는다(비HTTP 경로 포함).
- 허용 목록이 Sandbox 전체 단위라 "어느 리소스가 어느 호스트를 부르는가"는 구분하지 않는다.
- k3s 내장 정책 엔진은 새로 뜬 Pod를 잠깐 늦게 반영한다. 갓 만든 Pod에서 곧바로 트래픽을 내는 테스트는 실패할 수 있다(통합 테스트는 미리 띄운 프로브 Pod를 쓴다). 실제 Workflow 연결(14)에서 앱 Pod 준비 뒤 짧게 확인하는 단계가 필요할 수 있다.
- 앱 로그의 미지원 호출 패턴 목록 자체는 입력으로만 받는다(패턴 정의는 카탈로그 후속).
- Workflow·Activity 연결은 14.

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: NetworkPolicy는 Sandbox 워커가 생성, 미등록 출구는 차단해 의도 누락으로 처리하기로 결정.
- 2026-11-04(v2 동기화): main의 v2 구현(#22, #24, B-01 변경) 기준으로 수정.
- 2026-11-05: 구현. 프록시는 Sandbox 워커 소유로 확정하고 허용 목록은 Sandbox 단위로 둠.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [x] 구현·계약 정합성 및 최종 리뷰
- [x] 미검증 항목과 결과 증거 기록
