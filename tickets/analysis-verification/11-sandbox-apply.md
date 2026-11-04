# Ticket 11 — Sandbox 구성·적용·정리

## 상태
DRAFT — 사용자 검토 전

## 목적
Sandbox 워커가 구체 환경 값을 정하고 공통 차트로 Sandbox를 띄운 뒤, 끝나면 반드시 정리한다.

## 관련 요구사항
AV-011, AV-016, AV-020

## 선행 작업
Blocked by: 02B, 07, Provisioning 공통 렌더 v2(#24, DONE)

## 작업 범위
- 포함: 로컬 k3d Sandbox(`cmp-sandbox`), `prepare_sandbox`(Namespace·Release 이름 규칙, capabilities에서 StorageClass, DB 비밀번호 Secret 무작위 생성, `.env`와 예외 목록으로 앱 env Secret 생성, 변수별 `env_bindings`), `SharedDeploymentRequestV2` 구성과 `render_shared_deployment_v2` 호출, DB 이미지는 D-11 결과(그 전까지 공식 `postgres:16` Digest 고정값), `helm upgrade --install --wait`, 준비 검사(Pod Ready·`SELECT version()`·SQL·헬스), Namespace 삭제 정리, 실행 ID·만료 라벨, 만료 정리 작업
- 제외: 제자리 갱신·데이터 초기화(11B), NetworkPolicy(13), 내부 Ingress(차트 미지원), GKE/EKS Sandbox

## 수용 기준
- [ ] 첫 적용 후 DB·앱 Pod Ready, 요구 버전 확인, SQL 접속, 앱 헬스가 통과한다.
- [ ] 앱 env Secret에 예외 목록의 제외 변수와 DB 연결 변수가 없고, DB 변수는 리소스 출력 참조로 연결된다.
- [ ] `.env` 값과 DB 비밀번호가 로그·결과·Workflow 이력에 없다.
- [ ] 같은 실행의 재시도에서 DB 비밀번호 Secret을 다시 만들지 않는다.
- [ ] 통과·실패·취소 모두에서 Namespace와 보존 PVC까지 지워진다.

## 테스트 전략
- 제안 Test Seam: 격리 k3d + Helm에서 `prepare_sandbox`·`apply_sandbox`·`teardown_sandbox` 결과와 클러스터 상태. 잡는 것: 실제 구성·적용·정리. 못 잡는 것: 클라우드 StorageClass·Ingress Controller 차이.
- 실행: `uv run pytest tests/integration/sandbox -m k3d`, `make check`

## 완료 시 확인할 동작
fixture 요구사항·이미지로 Sandbox가 뜨고 준비 검사 후 정리까지 끝난다.

## 결정 대기
- D-08 Sandbox 기반 클러스터 준비 주체
- D-11 DB 이미지를 고르는 대상 구성 단계의 소유자

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 호출부 결정 방식(구체 값·Secret 생성은 Sandbox 워커)과 계약 v2 반영. 갱신·초기화는 11B로 분할. v2 동기화로 `render_shared_deployment_v2` 사용.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
