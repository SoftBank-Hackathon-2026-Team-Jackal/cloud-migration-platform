# Ticket 01 — 트래픽 전환 제어 로직

## 상태

DONE(로컬 검증, PR 전) — [Issue #61](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/61)

## 목적

가중치 단계·게이트·롤백 판단을 인프라 없이 시험할 수 있는 순수 모듈(`cmp_traffic`)로 만든다.

## 포함 범위

- `TrafficPolicy`·`Handover`·`Sample` 계약, 게이트 판정 `evaluate_step`, 실행 순서 `TrafficShifter`, 인계값 확인 `check_handover`
- 같은 단계 안의 부족한 관측 창 누적, p99 절대 하한, DB 게이트 옵션

## 수용 기준

- [x] 정책을 받아 단계별 결정(advance/hold/rollback/done)을 돌려준다.
- [x] 게이트 위반·적용 미확인·관측 실패 시 원본 100%로 롤백하고, 롤백 실패는 FAILED로 알린다.
- [x] 인계값이 READY가 아니거나 도착지가 없으면 아무것도 호출하지 않고 BLOCKED.
- [x] 인증 정보가 든 주소를 거부하고 결과에 주소가 남지 않는다.

테스트: `tests/traffic/test_traffic_shifter.py`.

## 관련 문서

[ADR-006](../../docs/decisions/ADR-006.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
