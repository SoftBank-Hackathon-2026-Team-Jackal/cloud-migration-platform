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
- [x] 실제 중계 서버(EC2) + WireGuard 터널 + 온프레 역할 VM에서 정상 전환·롤백 시험(ADR-006 기록).
- [x] 중계 서버 WireGuard peer 동기화: 플랫폼 `GET /relays/{relay_id}/agents`(RelayAgentList) 형식에 맞춰 단위 시험(`python -m cmp_traffic.peers`). 실제 플랫폼에 중계 서버를 등록해 받아 보는 시험은 남았다.
- [x] 상위 Workflow 연결(#128): 시작 입력의 `traffic`(`relay_queue`, `policy`)이 있으면 `MigrationWorkflow`가 `shift_traffic` 대신 `TrafficShiftWorkflow`를 자식으로 시작한다(패치 `traffic-shift-v1`). 완료는 검증 단계로, 롤백·막힘·실패는 `traffic_rolled_back`·`traffic_blocked`·`traffic_failed`로 끝난다. DB 게이트는 부모의 `db_gate_ok()`. 준비 단계에서 중계 서버 Envoy의 대상 주소가 인계값과 같은지 확인하고 다르면 `TargetMismatch`로 막는다. 실제 중계 서버·온프레(OCI)에서 정방향·롤백 확인(11/6).
- [ ] 인계값으로 중계 서버의 대상 주소를 바꾸는 동적 설정(지금은 bootstrap의 고정 주소와 일치해야 한다), 컨트롤러와 중계 서버가 다른 호스트일 때의 설정 배포(데모는 edge gRPC+TLS)

## 관련 문서

[ADR-006](../../docs/decisions/ADR-006.md) · [도메인 경계](../../docs/architecture/domain-boundaries.md)
