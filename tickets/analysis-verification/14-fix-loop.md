# Ticket 14 — 실패 분류·Fixer 루프·최종 발행·Draft PR

## 상태
DRAFT — 사용자 검토 전

## 목적
실패 원인을 나눠 Fixer가 고치고, 같은 Sandbox를 갱신해 재검증하며, 통과 시 최종 분석 결과를 발행하고 코드 수정은 Draft PR로 돌려준다.

## 관련 요구사항
AV-015, AV-017, AV-019, AV-020

## 선행 작업
Blocked by: 08, 08B, 10, 11B, 13

## 작업 범위
- 포함: `classify_failure`(SPEC(의도 누락 포함)/CODE/BUILD/RENDER), `fix_mapping`(요구사항·intent 패치)·`fix_code`(Fixer, 테스트 읽기 전용), mock을 실제 Activity로 바꾼 루프, 3회 초과 사람 검토, RENDER 중단 보고, `publish_final_report`, 이전용 브랜치 Draft PR과 해시 대조
- 제외: GitHub App 권한 발급

## 수용 기준
- [ ] 포트 오류 주입 → SPEC 분류 → 재렌더·갱신(빌드 없음) → 통과가 이력에 남는다.
- [ ] intent의 `external_host` 항목 누락 주입 → 출구 차단 → SPEC 분류 → intent 수정 → 통과가 이력에 남는다.
- [ ] 앱 코드 오류 주입 → CODE 분류 → 새 묶음·새 Digest → 통과가 이력에 남는다.
- [ ] 렌더 결과가 요구사항과 다르면 수정 없이 중단되고 보고가 남는다.
- [ ] Fixer가 테스트 파일을 바꾸려 하면 거절된다.
- [ ] 통과 시 발행된 최종 요구사항·소스 참조가 마지막으로 검증한 것과 같다.
- [ ] 루프 중 매핑 처리가 바뀐 행이 있으면 발행 전에 그 행만 다시 승인받는다(08B 게이트 재사용). 바뀐 행은 이전 분석 버전과 행 ID로 비교해 찾는다.
- [ ] 통과한 테스트에 대응하는 매핑 행의 검증 상태가 `SANDBOX_VERIFIED`로 올라간다.
- [ ] Draft PR 커밋으로 만든 묶음 해시가 통과한 묶음 해시와 같다.

## 테스트 전략
- 제안 Test Seam: k3d Sandbox + 가짜 LLM(정해진 수정 반환)으로 Workflow 이력. 잡는 것: 분류·루프·갱신·발행 경로. 못 잡는 것: 실제 모델의 수정 품질.
- 실행: `uv run pytest tests/sandbox/test_fix_loop.py`, `make smoke-temporal`

## 완료 시 확인할 동작
오류를 넣은 데모 앱이 루프를 거쳐 통과하고 최종 분석 결과와 Draft PR 정보가 남는다.

## 결정 대기
- Draft PR용 GitHub 권한(GitHub App 범위)

## 관련 문서
- [요구사항](../../specs/analysis-verification/spec.md) · [구현 계획](../../specs/analysis-verification/plan.md) · [티켓 목록](../../specs/analysis-verification/tasks.md)
- [제품 정책](../../docs/product/policies.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Temporal 흐름](../../docs/architecture/temporal-flow.md)
- [Planning 입력 계약 v2](../../specs/provisioning/input-contract-v2.md) · [ADR-003](../../docs/decisions/ADR-003.md)

## 설계 변경 기록
- 2026-11-04: 의도 누락 분류와 통과 시점 최종 발행 추가.
- 2026-11-04(목적 점검): 변경 행 재승인(08B)과 매핑 행 `SANDBOX_VERIFIED` 갱신 추가.
- 2026-11-04(01 리뷰 반영): 바뀐 행 판별을 행 ID 비교로 명시.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
