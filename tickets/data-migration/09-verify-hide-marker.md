# Ticket 09 — 전환 검증 결과에서 내부 marker 테이블 제외

## 상태

DONE(로컬 검증).

## 배경

ticket 08 로컬 e2e에서 `cutover.verify`에 전환 도구가 만든 `cmp_switchover_marker`가 사용자 테이블처럼 보였다. 이 테이블은 PAUSE 중 마지막 변경이 새 주 DB에 도착했는지 확인하는 용도라 사용자에게 보일 이유가 없고, `data.verify` rows 합계에도 섞였다.

## 구현

- `cmp_data.cutover_events.INTERNAL_TABLES`(`MARKER_TABLE`의 전체 이름과 테이블 이름)를 `cutover.verify` 행과 `data.verify` rows 합계에서 뺀다.
- 실행기(`switchover.py`)의 비교와 `verify_ok`는 그대로 둔다. marker 테이블 불일치도 전체 검증 실패로 남는다.

## 시험

- `tests/data/test_data_cutover_events.py::test_internal_marker_table_is_not_shown_as_a_user_table`
- 전체 `uv run pytest`: 2835 passed, 31 skipped
