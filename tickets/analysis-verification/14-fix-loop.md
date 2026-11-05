# Ticket 14 — 실패 분류·Fixer 루프·최종 발행·Draft PR

## 상태
분할됨 — 14A(PR #107)·14B(PR #110)·14C(PR #112) 모두 병합. 이 파일은 분할 전 전체 범위의 기록이다.

## 목적
실패 원인을 나눠 Fixer가 고치고, 같은 Sandbox를 갱신해 재검증하며, 통과 시 최종 분석 결과를 발행하고 코드 수정은 Draft PR로 돌려준다.

## 관련 요구사항
AV-015, AV-017, AV-019, AV-020

## 선행 작업
Blocked by: 08, 08B, 10, 11B, 13

## 작업 범위
- 포함: `classify_failure`(SPEC(의도 누락 포함)/CODE/BUILD/RENDER/INFRA), `fix_mapping`(요구사항·intent 패치)·`fix_code`(Fixer, 테스트 읽기 전용), mock을 실제 Activity로 바꾼 루프, 3회 초과 사람 검토, RENDER 중단 보고, `publish_final_report`, 이전용 브랜치 Draft PR과 해시 대조
- 부모 종료 상태 값 확정: 02B 골격의 임시 값(`needs_review`, `stopped_render`)을 검토 대기(진행 중 플래그)와 종료 상태(완료·실패·취소)로 정리한다. 민서님께 새 값을 공유하고 API의 SSE 종료 조건(`routes/migrations.py`) 반영을 요청한다. 계약상 `phase`는 문자열이라 OpenAPI 변경은 필요 없다. 부모 Workflow 변경은 종료 상태 값 반환에 한정하고 필요하면 버전 분기를 적용한다.
- 제외: GitHub App 권한 발급

## 수용 기준
- [ ] 포트 오류 주입 → SPEC 분류 → 재렌더·갱신(빌드 없음) → 통과가 이력에 남는다.
- [ ] intent의 `external_host` 항목 누락 주입 → 출구 차단 → SPEC 분류 → intent 수정 → 통과가 이력에 남는다.
- [ ] 앱 코드 오류 주입 → CODE 분류 → 새 묶음·새 Digest → 통과가 이력에 남는다.
- [ ] 렌더 결과가 요구사항과 다르면 수정 없이 중단되고 보고가 남는다.
- [ ] 클러스터 접근 불가·레지스트리 접근 실패를 주입하면 INFRA로 분류되고 Fixer가 호출되지 않는다.
- [ ] Fixer가 테스트 파일을 바꾸려 하면 거절된다.
- [ ] 통과 시 발행된 최종 요구사항·소스 참조가 마지막으로 검증한 것과 같다.
- [ ] 루프 중 매핑 처리가 바뀐 행이 있으면 발행 전에 그 행만 다시 승인받는다(08B 게이트 재사용). 바뀐 행은 이전 분석 버전과 행 ID로 비교해 찾는다.
- [ ] 통과한 테스트에 대응하는 매핑 행의 검증 상태가 `SANDBOX_VERIFIED`로 올라간다.
- [ ] Draft PR 커밋으로 만든 묶음 해시가 통과한 묶음 해시와 같다.
- [ ] 루프 결과(통과, 사람 검토 대기, RENDER 중단, 실패, 취소)가 정해진 종료 상태로 부모에 반영되고, 검토 대기는 종료 상태가 아니다. 기존 `scaffold_complete`·`cancelled` 동작과 기존 이력 replay는 유지된다.
- [ ] 새 종료 상태 값과 부모 변경 범위, replay 결과를 PR 설명에 기록해 민서님께 공유하고 SSE 종료 조건 반영을 요청한다.

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
- 2026-11-05: 14A·14B·14C로 분할하고 Draft PR은 GitHub App 없이 패치 산출물로 바꾸기로 결정. 각 조각의 수용 기준은 해당 티켓을 따른다.
- 2026-11-04: 의도 누락 분류와 통과 시점 최종 발행 추가.
- 2026-11-04(목적 점검): 변경 행 재승인(08B)과 매핑 행 `SANDBOX_VERIFIED` 갱신 추가.
- 2026-11-04(01 리뷰 반영): 바뀐 행 판별을 행 ID 비교로 명시.
- 2026-11-04(INFRA·D-07 반영): INFRA 분류와 Fixer 미호출 수용 기준 추가.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
