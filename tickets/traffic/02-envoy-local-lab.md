# Ticket 02 — Envoy 가중치 전환·롤백 로컬 시험

## 상태

DONE(로컬 검증, PR 전) — [Issue #62](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/62)

## 목적

Envoy 어댑터와 로컬 compose에서 실제 전환·롤백을 검증한다.

## 포함 범위

- `EnvoyProxy`(RDS 원자 교체 + 적용 확인), `EnvoyObserver`(관리 API 지표 차이), `infra/local/traffic-lab`(Envoy + 백엔드 2개 + 시나리오 드라이버)
- 시나리오: 정상 전환, 5xx 주입 롤백, 지연 주입 p99 롤백

## 수용 기준

- [x] 가중치가 단계별로 바뀌고 요청 분배가 맞는다.
- [x] 대상 장애·지연 주입 시 게이트가 넘어 롤백된다.
- [x] 단위 테스트(`tests/traffic/test_traffic_envoy.py`)는 가짜 관리 API로 적용 확인·거부·시간 초과를 검증한다.

실행: `infra/local/traffic-lab/compose.yaml` 헤더 참고. 실제 WAN 지연·NAT 뒤 온프레는 검증하지 못한다.

## 관련 문서

[ADR-004](../../docs/decisions/ADR-004.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
