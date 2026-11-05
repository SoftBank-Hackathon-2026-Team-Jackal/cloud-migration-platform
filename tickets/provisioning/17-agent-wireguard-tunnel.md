# Ticket 17 — Agent WireGuard 연결과 터널 노출 검증

## 상태
DONE — [Issue #71](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/71). 15·16 완료(main `c7ca25c`) 뒤 2026-11-05 Traffic 담당(이상헌님) 범위 답변으로 착수해 격리 k3d + 테스트용 WireGuard 상대로 수용 기준을 확인했다. 실제 AWS 중계 서버 경로는 미검증(상헌님과 합동 시험 예정, 아래). 새 Agent 릴리스(`agent-v0.2.0`)는 사용자 승인 후 태그한다.

## 목적
노트북(온프레미스 K3s)의 Agent가 중계 서버로 WireGuard 터널을 직접 열고, 배포한 앱·DB를 터널 주소로만 노출한 뒤 터널 경유 접근을 확인해 보고한다. 노트북은 인바운드 포트를 열지 않는다.

## 관련 요구사항
PROV-009, PROV-013, PROV-017, PROV-018, PROV-021

## 선행 작업
Blocked by: 15, 16

## 작업 범위
- 포함
  - WireGuard 키 쌍 생성. 비밀키는 Agent Namespace Secret에만 두고 공개키를 16 API로 등록한다.
  - 16에서 받은 설정으로 터널 인터페이스를 올린다(MTU 1420, PersistentKeepalive). 설정 버전이 바뀌면 다시 적용한다(중계 서버 교체·키 교체).
  - 앱 포트와 5432를 **터널 IP에만** 바인딩한 전달기로 클러스터 내부 Service에 연결한다. LAN·인터넷 쪽에는 열지 않는다.
  - 터널 상태 감시와 자동 재연결, 16 API로 상태 보고(handshake 시각, 터널 경유 앱 헬스, DB 접속 확인).
  - 15의 설치 Chart에 터널 구성 요소를 추가한다(필요 권한 포함).
- 제외: 중계 서버의 Envoy·WireGuard 서버 구성, DNS, TLS, 데이터 복제·트래픽 전환 실행, UDP가 막힌 환경용 대체 터널(아래 결정 대기).

## 수용 기준
- [x] 노트북에 인바운드 포트 없이 Agent가 먼저 연결해 터널이 성립하고, 중계 서버 쪽에서 `터널IP:앱포트`로 앱 헬스 2xx, `터널IP:5432`로 DB 접속이 된다.
- [x] 터널 IP가 아닌 주소(노드 LAN IP 등)로는 앱·DB 포트에 접근할 수 없다.
- [x] 터널을 끊었다 복구하거나 중계 서버 공개키·endpoint가 바뀌면 사람 개입 없이 재연결하고 상태 보고가 갱신된다.
- [x] 터널 경유 확인이 실패하면 원인 코드가 보고되고 ON_PREM `external_endpoint`가 비어 있어 16의 인계 조건이 진행을 막는다.
- [x] 비밀키가 Secret 밖(로그, 보고, 플랫폼 DB)에 남지 않는다.

## Test Seam
- 격리 k3d + Docker로 띄운 테스트용 WireGuard 상대(중계 서버 역할, 실제 중계 서버 구현 아님) + 로컬 플랫폼 API.
- 확인: 터널 성립, 터널 경유 앱 헬스·`pg_isready`, LAN 쪽 차단, 터널 단절·복구, 상대 키 교체 후 재연결, MTU 1420에서 큰 패킷(DF) 통과.
- 놓치는 것: 실제 AWS 중계 서버, 행사장 NAT·방화벽, 먼 WAN 지연. 상헌님 중계 서버가 준비되면 실제 경로 검증을 추가 증거로 기록한다.

## 착수 결정 후보 (구현 시작 시 확정)
| ID | 후보 | 근거 |
| --- | --- | --- |
| W1 터널 실행 방식 | Agent Pod에 `hostNetwork` + `NET_ADMIN` 터널 컨테이너를 두고 노드 커널 WireGuard 사용. 커널 모듈이 없으면 userspace(`wireguard-go`)로 대체 | Linux 5.6+ 커널에 기본 포함. 15의 클러스터 내부 실행과 같은 Pod |
| W2 노출 방식 | 터널 IP에만 바인딩한 TCP 전달기 → ClusterIP Service. NodePort는 사용하지 않음 | NodePort는 LAN 인터페이스에도 열린다 |
| W3 접근 제한 | WireGuard AllowedIPs로 중계 서버 터널 IP에서 온 패킷만 받는다 | DB 포트 노출 범위 최소화 |

## 착수 결정 (2026-11-05)

| ID | 결정 |
| --- | --- |
| W1 터널 실행 | Agent Pod에 **별도 `tunnel` 컨테이너**(같은 이미지, `python -m cmp_providers.agent_tunnel`)를 둔다. 후보와 달리 `hostNetwork`는 쓰지 않는다: WireGuard 인터페이스는 netns 단위라 Pod netns 안에서 커널 WireGuard가 동작하고, 그래야 노드 LAN 인터페이스와 섞이지 않는다. `tunnel` 컨테이너만 UID 0 + `NET_ADMIN`·`NET_BIND_SERVICE`(나머지 drop, 권한 상승 금지, 읽기 전용 루트)이고 `agent` 컨테이너는 15 그대로 비루트·무권한이다. 모드 `auto`(기본)는 커널 WireGuard(`ip link add type wireguard`)를 먼저 시도하고, 실패하면 이미지에 넣은 `wireguard-go`(userspace, `/dev/net/tun` hostPath)로 대체한다. `kernel`·`userspace`로 고정할 수 있다. |
| W2 노출 | 터널 IP에만 바인딩한 TCP 전달기(Agent 프로세스 안, 연결마다 스레드) → ClusterIP/Headless Service DNS. NodePort·hostPort·LoadBalancer를 쓰지 않는다. 전달 대상은 Agent가 찾는다: `cmp.dev/environment-id` 주석이 있는(Agent 소유) Namespace의 Service 중 5432 포트는 DB, 노출 포트(16 `exposed_ports`)와 같은 포트는 앱이다. 앱 헬스 경로는 같은 이름 Deployment의 readinessProbe `httpGet.path`(12 앱 Chart가 승인 Plan의 `health_path`로 채움), 없으면 `/health`. 후보가 없으면 `FORWARD_TARGET_MISSING`, 둘 이상이면 `FORWARD_TARGET_AMBIGUOUS`로 그 포트를 열지 않는다. |
| W3 접근 제한 | 중계 서버 peer의 AllowedIPs를 플랫폼이 준 대역(`10.200.0.0/24`)이 아니라 **중계 서버 터널 IP /32**(대역의 첫 주소, `10.200.0.1/32`)로 좁힌다. 다른 Agent가 중계 서버를 거쳐 보내는 패킷은 WireGuard가 버린다. 전달기도 출발지가 중계 서버 터널 IP(또는 Agent 자신의 헬스 확인)인 연결만 받는다. 인터페이스 주소는 `터널IP/32`, 경로는 `10.200.0.1/32 dev wg0`만 둔다. |
| W4 키 | 비밀키는 Agent가 Python으로 만든다(X25519, RFC 7748 — 외부 의존성 없이 공개키 도출). Agent Namespace의 Helm 관리 밖 Secret `cmp-agent-wireguard`(`private_key`)에만 저장하고, `kubectl apply -f -`와 `wg set ... private-key /dev/stdin`으로 표준 입력으로만 넘긴다. 시작할 때마다 공개키를 `PUT .../tunnel/key`로 등록(같은 키면 변화 없음)한다. Secret이 지워지면 새 키를 만들어 교체 등록한다(터널 IP는 16 규칙대로 유지). |
| W5 설정 적용 | `intervalSeconds`(기본 15초)마다 `GET .../tunnel/config`를 읽어 원하는 상태(중계 서버·AllowedIPs·MTU·Keepalive·전달 대상)를 계산하고 적용된 상태와 다르면 다시 적용한다. `config_version`이 바뀌면(중계 서버 교체·키 교체) 반드시 이 경로를 탄다. 중계 서버가 여럿이면 `relay_id` 순 첫 번째만 쓴다(같은 AllowedIPs를 두 peer가 가질 수 없음). 플랫폼이 잠시 안 되면 마지막 터널을 그대로 유지한다. |
| W6 감시·재연결 | `wg show wg0 latest-handshakes`로 마지막 handshake를 읽는다. 180초(WireGuard Reject-After-Time) 넘게 없으면 끊긴 것으로 보고 peer를 다시 설정(endpoint DNS 재해석)하고, 두 번 연속이면 인터페이스를 지우고 다시 만든다. 복구 자체는 PersistentKeepalive 25초로 WireGuard가 다시 handshake 한다. |
| W7 상태 보고 | 매 주기 `POST .../tunnel/status`. `app_health_ok` = handshake 정상 + 터널 IP의 전달기를 거친 앱 헬스 2xx(모든 앱 포트), `db_connect_ok` = handshake 정상 + 터널 IP:5432 전달기를 거친 PostgreSQL `SSLRequest` 응답(`S`/`N`, `pg_isready`와 같은 수준). Agent 혼자서는 중계 서버 쪽에서 들어오는 경로를 직접 시험할 수 없으므로 "터널 성립(handshake) + 터널 주소 전달기 동작"으로 판정한다. 중계 서버 쪽 실제 경로는 Envoy 헬스 체크(Traffic)와 k3d 시험의 테스트 peer가 확인한다. |
| W8 원인 코드 (16 API 추가) | 수용 기준 "원인 코드가 보고"를 위해 `TunnelStatusReport`에 선택 필드 `code`(대문자 코드, 성공이면 `null`)를 더하고 `agent_tunnel_status.code` 열(Alembic `20261105_05`)에 저장한다. 하위 호환(생략 가능)이며 인계 조건은 바뀌지 않는다. 코드: `HANDSHAKE_MISSING`, `RELAY_NOT_CONFIGURED`, `TUNNEL_INTERFACE_FAILED`, `APP_PORT_NOT_ASSIGNED`, `FORWARD_TARGET_MISSING`, `FORWARD_TARGET_AMBIGUOUS`, `APP_HEALTH_FAILED`, `DB_CONNECT_FAILED`, `INVALID_TUNNEL_CONFIG`, `FORWARDER_BIND_FAILED`, `KUBERNETES_UNAVAILABLE`. |
| W9 폐기·종료 | 플랫폼이 401/403이면 터널 인터페이스를 지우고 종료 코드 2로 멈춘다(15 H6와 같음). SIGTERM이면 전달기를 닫고 인터페이스를 지운다. |

## 결정 대기 (답변 기록)
- **#63과의 범위 중복 — 해소 (2026-11-05, 이상헌님 답변)**
  - Traffic이 중계 서버 EC2(Envoy + WireGuard **서버**)를 만들고, `GET /relays/{relay_id}/agents`를 폴링해 peer를 반영한다.
  - 온프레미스 Agent는 WireGuard **클라이언트**다. 먼저 아웃바운드로 연결하고 `터널IP:앱포트`(포트 그대로, 예 8080)와 `터널IP:5432`를 K3s Service로 전달한다.
  - 터널 대역 `10.200.0.0/24`, 중계 서버 `10.200.0.1`, MTU 1420, `PersistentKeepalive = 25`.
  - Agent는 중계 서버 endpoint·공개키를 `PUT .../tunnel/key` 응답(및 `GET .../tunnel/config`)에서 받는다.
- **UDP·WireGuard 포트가 막힌 네트워크 대응 — 이 티켓 범위 밖, Traffic 담당과 결정 대기.** WireGuard UDP 443 사용 여부나 TCP 기반 대체 터널(Cloudflare Quick Tunnel 등)은 중계 서버 쪽 구성과 함께 정해야 한다. 이 티켓은 UDP WireGuard만 구현한다. 행사장 네트워크에서 UDP가 막히면 `HANDSHAKE_MISSING`이 보고되고 ON_PREM Traffic 인계가 막힌다.

## 완료 시 확인할 동작
Agent를 설치한 노트북이 공개 포트 없이 중계 서버와 터널을 맺고, 터널 주소로 앱·DB가 확인된 결과가 플랫폼에 보고된다.

## 관련 문서
- [15](15-agent-runtime-install.md) · [16](16-tunnel-config-exchange.md) · [12](12-k3s-approved-rollout.md) · [Migration Network Preflight](../../specs/migration-network-preflight/spec.md)
- [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 2026-11-05 사용자 결정: 외부 연결이 가능한 Agent는 Provisioning 범위다. 중계 서버 내부는 Traffic 담당이다.
- 참고: 상헌님 실측(11/3~4) — MTU 8921에서는 큰 패킷 100% 유실, 1420 고정 필요. 피어 재등록 시 복구 4~35초, 인터페이스 재기동 시 약 6초.

## 완료 체크리스트
- [x] 수용 기준과 Test Seam 확인
- [x] 관련 테스트 통과와 회귀 확인
- [x] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰 (조정 세션 리뷰 대기)
- [x] 미검증 항목과 결과 증거 기록

## 구현 (2026-11-05)

- `cmp_providers.agent_tunnel`: X25519 키 생성·공개키 도출(RFC 7748, 의존성 없음), 키 Secret 저장소, 설정 → 원하는 상태(`plan_from_config`, W3·W5), 전달 대상 찾기(`discover_forwards`, W2), 터널 IP 전용 TCP 전달기(`TcpForwarders`, 출발지 검사), 커널/`wireguard-go` 인터페이스(`WireGuardLink`, W1), 감시·재연결·보고(`TunnelManager`, W6·W7), 실행 루프(`run_tunnel`, W9).
- `AgentControlClient`: `register_tunnel_key`(PUT)·`tunnel_config`(GET)·`report_tunnel_status`(POST).
- 16 API(W8): `TunnelStatusReport.code`(선택), `agent_tunnel_status.code` 열, Alembic `20261105_05`. OpenAPI는 이 필드만 추가.
- Chart: `tunnel` 컨테이너·`tunnel.*` 값·스키마, Namespace `list` 권한. 이미지: `iproute2`·`wireguard-tools`·`wireguard-go`(0.0.20250522 커밋 고정).
- 문서: [Agent 설치 §7](../../specs/provisioning/onprem-agent-install.md#7-wireguard-터널-17), [공통 배포 계약](../../specs/provisioning/onprem-shared-deployment.md).
- 재현 스크립트: `scripts/smoke_onprem_agent_tunnel.py`.

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_agent_tunnel.py` | 35개 통과: RFC 7748 벡터, 키 1회 생성·재사용·공개키만 등록, AllowedIPs `/32` 축소·첫 중계 서버·잘못된 설정 거절, 설정 버전 변경 시만 재적용, handshake 없음/180초 초과 → `HANDSHAKE_MISSING` → peer 재설정 → 인터페이스 재생성, 원인 코드(`APP_HEALTH_FAILED`·`DB_CONNECT_FAILED`·`FORWARD_TARGET_*`·`APP_PORT_NOT_ASSIGNED`·`RELAY_NOT_CONFIGURED`·`TUNNEL_INTERFACE_FAILED`), 플랫폼 중단 시 터널 유지, 폐기 시 정리·종료 코드 2, 로그·보고에 비밀 없음, `wg` 명령에 비밀키는 stdin으로만, 커널 실패 시 `wireguard-go`·`kernel` 고정 시 대체 없음, 소유 Namespace만 탐색·헬스 경로, 실제 loopback 전달기의 출발지 제한·다른 주소 미노출, PostgreSQL SSLRequest 탐지, 클라이언트 메서드 |
| `uv run pytest tests/test_tunnel_exchange.py tests/test_onprem_agent_chart.py` | W8 코드 저장·형식 검사·인계 불변, Chart `tunnel` 컨테이너 권한(`NET_ADMIN`·`NET_BIND_SERVICE`만, hostNetwork·포트 없음)·kernel 모드 tun 미마운트·끄기·값 검사 |
| `make check` | Ruff·전체 1199개 통과, 7개 skip |
| `make openapi` | `TunnelStatusReport.code`만 추가 |
| `alembic upgrade head` → `check` → `downgrade 20261105_04` → `upgrade head` (SQLite) | 차이 없음, 왕복 성공 |
| `docker build -f infra/onprem-agent/Dockerfile` (linux/arm64) | 성공. 이미지 안에서 Python 공개키 = `wg pubkey` 결과, `wireguard-go v0.0.20250522`, `wireguard-tools v1.0.20210914` |
| `scripts/smoke_onprem_agent_tunnel.py` (k3d v5.9.0 / K3s v1.36.4+k3s1 / ARM64 / Docker Desktop 커널 6.10, 전용 kubeconfig, 테스트 상대 = 같은 Docker 네트워크의 Alpine 컨테이너 커널 WireGuard `10.200.0.1`) | [증거](../../docs/verification/provisioning-17-k3s.json): ① 설치·승인 배포 READY → Agent가 먼저 연결해 handshake, 상태 `app/db OK, code null`, Traffic 인계 `10.200.0.2:8080`·Data `10.200.0.2:5432` 진행 ② 상대에서 터널로 `/health` 200, `pg_isready` accepting ③ 노드 IP:8080/5432, (Pod 대역 경로를 준) Agent Pod IP:8080/5432 모두 차단, 리스너는 `10.200.0.2:5432`·`:8080`뿐 ④ wg0 MTU 1420, DF 1392+28 통과·1393 거절 ⑤ 상대에서 peer 삭제 → 목록 폴링 재등록으로 18초 안에 복구 ⑥ 상대 UDP 201초 차단 → `HANDSHAKE_MISSING`·인계 `EXTERNAL_ENDPOINT_UNAVAILABLE`·`PEER_RESET`, 해제 8초 뒤 정상 보고 ⑦ 상대 키·endpoint(포트) 교체 + 재등록 → 설정 버전 2 재적용, 5초 뒤 정상 ⑧ 비밀키는 `cmp-agent-wireguard`(Helm 관리 밖)에만, Agent 로그·플랫폼 DB·중계 서버 목록·증거에 없음 ⑨ `tunnel.mode=userspace` → `wireguard-go`(tun) 같은 키로 정상 ⑩ 폐기 → `tunnel` 종료 코드 2, 상대 peer 제거, 터널 접근 불가 ⑪ Release·Namespace·상대 컨테이너 삭제, 기본 kubeconfig 불변 |

TDD 메모: Agent 로직·클라이언트·Chart·W8 API는 테스트를 먼저 썼다(W8 저장 테스트 RED → GREEN, Chart 4개 RED → GREEN). k3d 첫 실행에서 `docker pause`로는 커널 WireGuard가 끊기지 않아(프로세스만 멈춤) 단절 시험이 무효임을 발견해 iptables UDP 차단으로 바꿨다. 같은 실행에서 단일 스레드 테스트 앱이 잠시 unready가 됐고 Agent가 `APP_HEALTH_FAILED`로 보고한 뒤 회복했다(앱 쪽 현상). 두 번째 실행은 첫 "터널 정상" 대기(240초)에서 시간 초과로 끝났고 진단 정보가 남지 않았다. 실패 시 상태·코드를 남기도록 스크립트를 고친 뒤 세 번째 실행이 위 전 단계를 통과했다. 두 번째 실행의 원인은 확인하지 못했다(재현되지 않음).

정리: `k3d cluster delete cmp-t17`(Registry `cmp-t17-reg` 포함), `k3d cluster list`·`registry list` 비어 있음, `t17` 컨테이너 없음, 기본 kubeconfig SHA-256 전후 동일. 로컬 Docker에 `cmp-onprem-agent:t17`·`:t17b`, `cmp-t17-relay-peer:test`, `localhost:5517/*` 태그가 남아 있다. 검증 중 플랫폼 API를 `0.0.0.0`의 임의 포트로 잠시 열었다(15와 같음).

### 미검증·제한 사항
- **실제 AWS 중계 서버 경로**: 상헌님 중계 서버(EC2 WireGuard 서버 + Envoy)·EIP·보안 그룹, 행사장 NAT·방화벽, WAN 지연. 합동 시험으로 추가 증거를 남긴다(2026-11-06 목표).
- UDP가 막힌 네트워크 대체 터널(범위 밖, Traffic과 결정 대기).
- Agent 혼자서는 중계 서버 쪽에서 들어오는 요청을 시험할 수 없어, 앱 헬스·DB 확인은 "handshake 정상 + 터널 주소 전달기 동작"으로 판정한다(W7). 중계 서버 쪽 확인은 Envoy 헬스 체크 몫이다.
- 중계 서버가 여럿이면 첫 번째만 쓴다(W5). Envoy 요청 출발지가 `10.200.0.1`이 아니면 Agent가 받지 않는다(W3) — 상헌님과 확인 필요.
- Pod Security `baseline` 이상을 강제하는 클러스터, x86_64·다른 K3s 배포판, 장시간 실행, 커널 WireGuard가 없는 실제 호스트(대체 경로는 `userspace` 고정으로만 확인).
- `agent-v0.2.0` 릴리스(GHCR 이미지·OCI Chart)는 사용자 승인 대기.
