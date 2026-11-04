# Ticket 16 — 데모 앱 통합 검증

## 상태
DRAFT — 사용자 검토 전

## 목적
데모 앱과 실제 EKS 원본으로 첫 사이클 전체를 돌려 통합 수용 조건을 확인한다.

## 관련 요구사항
AV-001~AV-022, spec §7

## 선행 작업
Blocked by: 06, 15, 데모 앱 준비

## 작업 범위
- 포함: 데모 앱 실제 분석, 실제 빌드, 미리 띄운 Sandbox 기반에서 검증, 소요 시간 측정, 시연 순서
- 제외: Production 배포·데이터 이전·트래픽(각 담당)

## 수용 기준
- [ ] spec §7의 1~9가 실제 환경에서 확인된다.
- [ ] 분석·검증이 5분 안에 끝나거나, 넘으면 단계별 시간과 사전 준비 범위가 기록된다.
- [ ] 결과·로그·이력에서 비밀 값 검색 결과가 0건이다.

## 테스트 전략
- 제안 Test Seam: 실제 환경 실행 기록(Workflow ID, 단계별 시간, 결과 참조). 잡는 것: 통합 동작. 못 잡는 것: 다른 앱 구성.
- 실행: 데모 실행 스크립트, 결과 증거를 이 티켓에 기록

## 완료 시 확인할 동작
데모 앱을 넣으면 검증된 Digest와 분석 결과가 Planning으로 넘어간다.

## 결정 대기
- 데모 앱이 첫 사이클 지원 언어로 준비됨(D-07)
- Sandbox 기반 클러스터 준비(D-08)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 의도 누락 시나리오를 통합 조건에 포함.
- 2026-11-04(v2 동기화): main의 v2 구현(#22, #24, B-01 변경) 기준으로 수정.
- 2026-11-04(목적 점검): 통합 조건 범위를 spec §7 1~9(손실 승인 조건 포함)로 갱신.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
