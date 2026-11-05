# Ticket 17 — Agent WireGuard 연결과 터널 노출 검증

## 상태
TODO — [Issue #71](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/71). 15·16 완료 후 착수한다.

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
- [ ] 노트북에 인바운드 포트 없이 Agent가 먼저 연결해 터널이 성립하고, 중계 서버 쪽에서 `터널IP:앱포트`로 앱 헬스 2xx, `터널IP:5432`로 DB 접속이 된다.
- [ ] 터널 IP가 아닌 주소(노드 LAN IP 등)로는 앱·DB 포트에 접근할 수 없다.
- [ ] 터널을 끊었다 복구하거나 중계 서버 공개키·endpoint가 바뀌면 사람 개입 없이 재연결하고 상태 보고가 갱신된다.
- [ ] 터널 경유 확인이 실패하면 원인 코드가 보고되고 ON_PREM `external_endpoint`가 비어 있어 16의 인계 조건이 진행을 막는다.
- [ ] 비밀키가 Secret 밖(로그, 보고, 플랫폼 DB)에 남지 않는다.

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

## 결정 대기
- **Traffic 티켓 [#63](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/63)과의 범위 중복**: #63은 "WireGuard 아웃바운드 터널(MTU 1420 고정)"과 중계 서버 배포 자산을 범위로 둔다. 이 티켓은 온프레미스(노트북) 쪽 WireGuard 클라이언트를 Agent가 맡고, 중계 서버 쪽 WireGuard 서버·peer 반영은 #63이 맡는 것으로 나눠야 한다. 합의 전에는 착수하지 않는다.
- UDP나 WireGuard 포트가 막힌 네트워크(행사장 Wi-Fi 등) 대응: WireGuard UDP 443 사용 여부, TCP 기반 대체 터널(실측한 Cloudflare Quick Tunnel 등) 필요 여부. Traffic 담당과 함께 정한다.

## 완료 시 확인할 동작
Agent를 설치한 노트북이 공개 포트 없이 중계 서버와 터널을 맺고, 터널 주소로 앱·DB가 확인된 결과가 플랫폼에 보고된다.

## 관련 문서
- [15](15-agent-runtime-install.md) · [16](16-tunnel-config-exchange.md) · [12](12-k3s-approved-rollout.md) · [Migration Network Preflight](../../specs/migration-network-preflight/spec.md)
- [테스트 전략](../../docs/engineering/testing-strategy.md)

## 설계 변경 기록
- 2026-11-05 사용자 결정: 외부 연결이 가능한 Agent는 Provisioning 범위다. 중계 서버 내부는 Traffic 담당이다.
- 참고: 상헌님 실측(11/3~4) — MTU 8921에서는 큰 패킷 100% 유실, 1420 고정 필요. 피어 재등록 시 복구 4~35초, 인터페이스 재기동 시 약 6초.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록
