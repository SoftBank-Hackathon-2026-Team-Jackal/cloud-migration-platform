# Ticket 14C — 최종 발행·재승인·수정 패치 산출물

## 상태
DRAFT — 사용자 검토 전

## 목적
통과한 결과만 최종 분석 결과로 발행하고, 루프 중 바뀐 매핑은 다시 승인받으며, 코드 수정은 같은 해시의 패치 산출물로 남긴다.

## 관련 요구사항
AV-017, AV-019, AV-020

## 선행 작업
Blocked by: 14B

## 작업 범위
- 포함: `publish_final_report`, 변경 행 재승인(08B 게이트 재사용, 이전 분석 버전과 행 ID 비교), 통과한 테스트에 대응하는 매핑 행의 `SANDBOX_VERIFIED` 갱신, 수정 패치 산출물(패치·브랜치 이름·PR 제목과 본문·SHA-256 저장)과 소스 해시 대조, `DraftPrPublisher` 인터페이스와 기본 구현(전송 없이 "패치 준비됨" 기록)
- 제외: GitHub App 권한 발급, 실제 push·PR 생성(결정: GitHub App은 쓰지 않는다. 패치를 받은 사람이 직접 적용한다)

## 수용 기준
- [ ] 통과 시 발행된 최종 요구사항·소스 참조가 마지막으로 검증한 것과 같다.
- [ ] 루프 중 매핑 처리가 바뀐 행이 있으면 발행 전에 그 행만 다시 승인받는다.
- [ ] 통과한 테스트에 대응하는 매핑 행의 검증 상태가 `SANDBOX_VERIFIED`로 올라간다.
- [ ] 패치를 적용해 만든 소스 해시가 통과한 소스 해시와 같다.
- [ ] 패치 산출물에는 자격 증명이 들어가지 않고, 외부로는 아무것도 전송되지 않는다.

## 테스트 전략
- 제안 Test Seam: 발행 Activity와 패치 생성·해시 대조(가짜 Publisher). 잡는 것: 발행 일관성·재승인 범위. 못 잡는 것: 실제 GitHub 동작.
- 실행: `uv run pytest tests/sandbox/test_publish.py`

## 완료 시 확인할 동작
통과한 실행에서 최종 분석 결과와 수정 패치 산출물 참조가 남는다.

## 결정 대기
- 패치를 받아 PR을 여는 담당과 절차

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-05: 기존 티켓 14에서 분리. "Draft PR"을 "패치 산출물"로 바꿈(GitHub App 미사용).

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
