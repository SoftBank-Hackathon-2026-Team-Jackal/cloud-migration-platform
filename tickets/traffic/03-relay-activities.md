# Ticket 03 — 중계 서버 Activity 연결

## 상태

IN PROGRESS(로컬) — [Issue #63](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/63)

## 목적

Temporal에서 트래픽 전환을 Activity로 실행하고 `traffic.snapshot`을 낸다.

## 포함 범위

- `TrafficShiftWorkflow`(자식 Workflow)와 Activity 4개(`traffic_prepare`, `traffic_set_weight`, `traffic_observe`, `traffic_rollback`), 마이그레이션 Worker 등록
- `build_snapshot`: 프론트 `traffic.snapshot` 계약 필드
- 중계 서버 위치는 환경변수(`TRAFFIC_RDS_PATH` 등), 미설정이면 전환하는 척하지 않고 실패

제외: 상위 `MigrationWorkflow`의 `shift_traffic` 단계 교체(Provisioning 인계값 조회 연결 후), WireGuard 실제 구축, Route 53 DNS 최종 전환.

## 수용 기준

- [x] Activity가 Temporal에서 실행되고 단계마다 snapshot 필드가 만들어진다(가짜 Activity 시험).
- [x] 롤백 Activity가 원본 100%로 되돌린다.
- [ ] 상위 Workflow 연결과 실제 중계 서버(WireGuard) 시험은 인계값 형식 확정과 클라우드 환경 이후.

## 관련 문서

[ADR-004](../../docs/decisions/ADR-004.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
