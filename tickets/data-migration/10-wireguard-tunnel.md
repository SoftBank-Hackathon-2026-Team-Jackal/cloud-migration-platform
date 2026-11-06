# Ticket 10 — 플랫폼이 만드는 WireGuard 복제 경로(EKS ↔ GKE)

## 상태

DONE(로컬 검증: k3d 두 개 + 로컬 플랫폼). 실제 EKS ↔ Provisioning GKE 검증과 데모 서버 값은 남아 있다.

## 배경

데모 기본 대상인 ON_PREM은 Provisioning Agent 터널(prov 17)이 중계 서버 → 노트북 DB 한 방향만 연다. 대상 DB가 원본으로 구독하는 경로, 중계 서버의 DB 전달, Worker의 노트북 k3s 접근이 모두 없어서 바로 쓸 수 없다. 그래서 DB 이전 시연은 EKS → GKE 경로로 먼저 맞춘다(2026-11-06 사용자 결정 B). ticket 06에서 이 경로를 수동 스크립트(`infra/data-migration/crosscloud-lab/up.sh`)로 검증했지만, 웹에서 시작한 이전은 구축 직후 바로 데이터 단계로 가므로 플랫폼이 터널을 직접 만들어야 한다.

## 결정

| ID | 질문 | 결정 | 이유 |
| --- | --- | --- | --- |
| T1 | 언제 만드는가 | 데이터 자식 Workflow의 첫 단계 `dm_tunnel`(사전 점검 전). 입력에 `tunnel`이 있을 때만, `data-tunnel-v1` patch | 대상 클러스터는 구축이 끝나야 있다. 없으면 기존 흐름 그대로 |
| T2 | 어디에 두는가 | 각 클러스터의 `cmp-data-tunnel` Namespace에 게이트웨이 Deployment 하나(ticket 06 실습과 같은 alpine + wireguard-tools + socat, privileged) | 대상 앱 Namespace에는 Provisioning 기본 차단 정책이 있다 |
| T3 | 대상 DB 접근 | 대상 DB Namespace에 NetworkPolicy `<name>-db-in`(게이트웨이 → DB 5432만 허용)을 더한다 | NetworkPolicy는 허용만 더해진다. Provisioning 정책은 건드리지 않는다 |
| T4 | 입구 | 대상 쪽 UDP LoadBalancer 하나, `loadBalancerSourceRanges = source_egress_cidrs`(원본 NAT 공인 IP, IPv4 /16 이하) | 원본은 접속만 한다. 입구를 원본 NAT로만 좁힌다 |
| T5 | peer_host | 양쪽 모두 `<name>-peer-db.cmp-data-tunnel.svc.cluster.local:5432`. 입력의 `peer_host`·`target_peer_host`는 무시하고 원본 `peer_host`는 생략 가능. 원본 `local_host`는 필수, 대상 `local_host`는 `<release>.<ns>.svc.cluster.local` | 게이트웨이 Service 이름이 양쪽에서 같아 주소를 미리 정할 수 있다 |
| T6 | 이름 | `cmp-wg-<sha256(migration_id)[:8]>` | 같은 클러스터에서 이전 둘이 겹치지 않게 |
| T7 | 키 | Worker가 X25519 키를 만들어(`cmp_providers.agent_tunnel`과 같은 구현) 각 클러스터 Secret에만 넣는다. 있으면 그대로 쓴다. 결과·이력에는 LB 주소와 handshake 여부만 | 재실행해도 키가 바뀌지 않는다 |
| T8 | 실패 | LB 주소 대기 시간 초과 `TUNNEL_ENDPOINT_TIMEOUT`, handshake 대기 시간 초과 `TUNNEL_HANDSHAKE_TIMEOUT`(둘 다 재시도 가능, Activity 2회), kubectl 실패 `TUNNEL_COMMAND_FAILED` | |

## 구현

- `cmp_data.tunnel`: `WireGuardTunnel`(입력), 이름·`peer_host`, 게이트웨이 ConfigMap·Deployment·Service, UDP LB, 대상 DB 허용 정책(순수)
- `cmp_data.handoff`: `DataMigrationInput.tunnel`, `endpoints(view, given, migration_id)` → (원본, 대상, 코드)
- `MigrationWorkflow._data_migration`: `endpoints()`로 양쪽 주소를 정하고 `tunnel`을 자식 입력에 넣는다
- `DataMigrationWorkflow`: `tunnel` phase → `dm_tunnel`, `status.tunnel`
- `cmp_worker.activities.data_tunnel.dm_tunnel`: 키 → 대상 게이트웨이·LB·정책 → LB 주소 대기 → 원본 게이트웨이 → rollout → handshake

## 시험

- `tests/data/test_data_tunnel.py`(16개): 매니페스트(공개키만, LB 출발지 제한, DB 정책), 입력 검증, `endpoints()`(터널 있음·없음·막힘), Activity(가짜 클러스터: 순서, 키 재사용, 비밀키 미노출, LB 시간 초과), Workflow(터널이 사전 점검보다 먼저, 입력 없으면 호출 안 함)
- 로컬 e2e(2026-11-06): ticket 08과 같은 로컬 플랫폼(API·Temporal·대역 worker)에서 `WEB_START_DATA_MIGRATION`에 `tunnel: {source_egress_cidrs: [원본 노드 IP/32]}`만 주고 peer 주소는 넣지 않았다. k3s ServiceLB가 대상 LB 주소를 냈고, 게이트웨이가 handshake한 뒤 터널로 초기 복사·CDC → 승인 → 전환이 끝났다. 쓰기 중단 43.75ms(터널 없는 NodePort 경로 5.96ms), 테이블 3개 체크섬 일치, 전환 중 앱 쓰기 오류 0건, SSE 이벤트 56건 계약 통과. 준비 전 승인은 409(`phase: tunnel`).

## 남은 일

- 실제 EKS ↔ Provisioning GKE 검증(클라우드 비용·승인 필요). GKE LB(UDP passthrough)와 `loadBalancerSourceRanges`, EKS NAT 공인 IP 확인
- 데모 서버: GCP 대상 프로필과 GCP 연결(Provisioning), `WEB_START_DATA_MIGRATION`에 `tunnel` 값
- 정리: `finish`·취소 때 게이트웨이·LB·정책을 지우는 단계가 없다(LB 비용). 지금은 역방향 복제를 위해 남겨 둔다
- 관찰: 로컬 e2e에서 앱을 PgBouncer로 다시 시작한 직후 첫 쓰기 2건이 실패했다(ticket 06 `dm_route_app`, 전환과 무관). 원인 확인 필요
