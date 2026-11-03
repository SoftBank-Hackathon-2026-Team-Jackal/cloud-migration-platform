# Ticket 13 — 판정과 실행 기록

## 상태
DRAFT — 사용자 검토 전

## 목적
객관적 신호로 통과 여부를 정하고, 놓친 의존성을 잡기 위한 출구 기록을 모은다.

## 관련 요구사항
AV-013, AV-014

## 선행 작업
Blocked by: 11, 12

## 작업 범위
- 포함: `judge`(필수 테스트·미지원 호출 패턴·출구 기록·매핑 대조), 출구 프록시(허용 목록·기록), 프록시 로그 수집, 호출 지점별 테스트 대응 표시
- 제외: 커버리지 계측 이미지

## 수용 기준
- [ ] 필수 테스트 하나라도 실패하면 통과하지 않는다.
- [ ] 목록 밖 호스트(`*.amazonaws.com`, `169.254.169.254`) 시도가 1건이라도 있으면 통과하지 않고 호스트가 보고된다.
- [ ] 판정 이유가 사람이 읽을 수 있는 목록으로 남는다.
- [ ] 같은 입력이면 같은 판정이 나온다.

## 테스트 전략
- 제안 Test Seam: `judge(...)` 순수 함수 + k3d에서 출구 기록 수집. 잡는 것: 판정 규칙·외부 호출 감지. 못 잡는 것: 프록시를 우회하는 경로(NetworkPolicy 전까지).
- 실행: `uv run pytest tests/sandbox/test_judge.py`, k3d 통합 테스트

## 완료 시 확인할 동작
일부러 원본 S3를 부르는 앱 fixture가 판정에서 실패하고 호스트가 보고된다.

## 결정 대기
- NetworkPolicy로 프록시 외 egress 차단(11의 결정 대기와 연결)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Provision 입력 계약](../../specs/provisioning/input-contract.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 구현 중 변경: 아직 없음

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
