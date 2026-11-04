# Ticket 13 — 통신 규칙·판정·실행 기록

## 상태
DRAFT — 사용자 검토 전

## 목적
`network_intent`로 Sandbox NetworkPolicy와 출구 규칙을 만들고, 객관적 신호로 통과 여부를 정한다.

## 관련 요구사항
AV-013, AV-014

## 선행 작업
Blocked by: 08, 11, 12

## 작업 범위
- 포함: NetworkPolicy 생성(기본 차단, `network_intent`의 리소스 간 항목·외부 진입·DNS 허용, 외부는 출구 프록시만), 프록시 허용 목록(intent의 `external_host` 항목 + `KEEP` 서비스), `MIGRATE` 서비스 원본 주소·목록 밖 호스트 차단·기록, `judge`, 호출 지점별 테스트 대응 표시
- 제외: 커버리지 계측 이미지, Production 정책 변환(민서)

## 수용 기준
- [ ] `network_intent`에 없는 리소스 간 통신은 막힌다.
- [ ] intent의 `external_host`와 `KEEP` 서비스(첫 사이클 S3)로의 호출은 허용·기록된다.
- [ ] 원본 DB·원본 ALB 같은 `MIGRATE` 원본 주소와 목록 밖 호스트로의 시도는 차단·기록되고 판정이 실패하며 이유에 호스트가 나온다.
- [ ] 필수 테스트 하나라도 실패하면 통과하지 않는다.
- [ ] 같은 입력이면 같은 판정이 나온다.

## 테스트 전략
- 제안 Test Seam: `judge(...)` 순수 함수 + k3d에서 NetworkPolicy·프록시 기록 수집. 잡는 것: 판정 규칙·외부 호출 감지. 못 잡는 것: 프록시를 우회하는 비HTTP 경로.
- 실행: `uv run pytest tests/sandbox/test_judge.py`, k3d 통합 테스트

## 완료 시 확인할 동작
intent에서 외부 목적지를 지운 fixture가 판정에서 실패하고 호스트가 보고된다.

## 결정 대기
- Production(K3s)에서 NetworkPolicy가 필요해지면 공유 차트로 합칠지(민서)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: NetworkPolicy는 Sandbox 워커가 생성, 미등록 출구는 차단해 의도 누락으로 처리하기로 결정.
- 2026-11-04(v2 동기화): main의 v2 구현(#22, #24, B-01 변경) 기준으로 수정.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
