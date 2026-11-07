# Ticket 81 — 데이터 터널 게이트웨이가 재시작·NLB DNS 지연에서 회복하게

## 상태
IN_REVIEW (로컬 구현·시험 완료, PR 리뷰·데모 리허설 대기) — [Issue #321](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/321). 2026-11-07 사용자 지시로 착수. 담당 민서. 데이터 영역 코드(`cmp_data.tunnel`) 대행.

## 배경 (리허설 증거, 2026-11-07 `migration-cf7c5262-7852-4119-bbea-c09d2f5c8f76`)
- 80 반영 뒤 인계 대기는 12초 만에 통과했고 데이터 자식 Workflow가 `dm_tunnel`(ADR-007 2안, `ingress="source"`)을 시작했다.
- 원본 EKS: NLB(02:44:32.9 생성)·게이트웨이 Pod 정상. 맥 k3d: 게이트웨이 Pod가 02:44:38 시작 뒤 `Error`로 5회 이상 재시작, 로그 `Failed to create TUN device: invalid argument`. `dm_tunnel`은 handshake를 기다리며 멈췄다.
- **확인한 원인 (재시작 고리):** 로그에 wireguard-go의 "커널이 WireGuard를 지원한다" 안내가 나온다 → 커널 경로 `ip link add wg0`이 실패한 이유는 첫 실행이 만든 `wg0`이 같은 Pod 네트워크에 남아 있어서다. 대체 경로 `wireguard-go wg0`도 같은 이름으로 실패한다. 첫 실행이 한 번 실패하면 Pod가 스스로 회복하지 못한다.
- **추정한 원인 (첫 실패):** 게이트웨이가 NLB 생성 5초 뒤에 시작했다. 막 생긴 NLB의 DNS 이름은 보통 아직 풀리지 않으므로 `wg set … endpoint <NLB DNS>`가 실패하고 `set -eu`로 종료했을 것이다. 첫 실행 로그는 kubelet이 지워 직접 증거는 없다(노드에 `5.log`만 남음).
- 조치: k3d Pod를 지워 새 Pod(새 네트워크)로 띄우자 즉시 handshake, 실행이 사전 점검으로 넘어갔다.

## 작업 범위
- 포함: `cmp_data.tunnel.START_SCRIPT`
- 제외: `establish`의 대기·시간 초과(그대로), Agent 터널(별도 스크립트), 이미지

## 착수 결정 (2026-11-07, Issue #321)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| D1 | 남은 wg0 | 시작할 때 `ip link del wg0 2>/dev/null \|\| true` 먼저 | 컨테이너 재시작은 Pod 네트워크를 유지한다. 지우면 주소도 함께 사라져 `ip addr add`가 다시 성공한다 |
| D2 | endpoint 이름 | 피어 키·허용 IP는 먼저 설정하고, endpoint는 배경에서 풀릴 때까지 5초마다 다시 시도(`PEER_ENDPOINT_UNRESOLVED <host>` 로그) | 첫 실패를 직접 확인하지 못해도 DNS 지연을 흡수한다. 상한은 `dm_tunnel`의 handshake 대기(source 입구는 `endpoint_timeout_s`, 기본 600초)이고, 끝나면 정리 Activity가 Deployment를 지운다 |
| D3 | 준비 상태 | endpoint를 기다리지 않고 `/tmp/started`를 만든다 | `rollout_wait`가 DNS 대기로 시간 초과되지 않게. 실제 준비는 handshake로 확인한다(기존 설계) |

## 테스트 Seam
`tests/data/test_data_tunnel_start_script.py`: `START_SCRIPT`를 `/bin/sh`로 실행하고 `apk`·`ip`·`wg`·`wireguard-go`·`socat`·`mknod`·`sleep`을 가짜로 둔다(wg0은 파일, 남은 wg0이 있으면 wireguard-go 실패 — 데모 k3d와 같음). 실제 커널·DNS는 데모 k3d에서 확인.

## 수용 기준
- [x] 새 시작: 커널 경로, endpoint 설정
- [x] 남은 wg0이 있는 재시작에서도 성공(wireguard-go로 가지 않음) — 이전 스크립트에서는 실패
- [x] endpoint가 처음 몇 번 안 풀려도 준비 상태가 되고 풀리면 설정, 실패마다 로그
- [x] 입구 쪽(endpoint 없음)은 그대로, 커널 WireGuard가 없으면 wireguard-go 대체 경로 그대로
- [ ] 데모 리허설에서 게이트웨이 재시작 없이 handshake(병합·배포 뒤)

## 검증
`make check` 통과. 계약·HTTP 경로 변경 없음.
