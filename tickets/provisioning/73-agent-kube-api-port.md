# Ticket 73 — Agent 터널로 K3s API 포트 노출 (ADR-007 2안 2단계)

## 상태
IN_PROGRESS (로컬 구현·시험 완료, 리뷰 대기. 중계 서버 전달(4단계)·kubeconfig 발급(5단계)·Agent 이미지 릴리스·실측 남음) — [Issue #299](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/299). 2026-11-07 조율 세션 지시로 착수. 번호 70은 이미 [중계 서버 실시간 경로](70-relay-stream-routes.md)가 써서 73으로(71·72는 다른 세션) 한다.

## 배경
[ADR-007](../../docs/decisions/ADR-007.md) 2안에서 데이터 이전 worker는 `target_kubeconfig_ref`(access=KUBECONFIG, `packages/data/src/cmp_data/handoff.py`)로 맥 k3d API에 붙어 `kubectl exec … psql`을 한다. k3d는 NAT 뒤라 바로 닿지 않는다. 경로는 worker(데모 서버) → 중계 서버(4단계) → Agent 터널 IP:API 포트 → Agent 전달기 → `default/kubernetes` Service:443 이다. 이 티켓은 Agent·플랫폼 API 조각만 한다.

## 관련 요구사항
PROV-009, 017, 021, DM-001, DM-011, ADR-007, [16](16-tunnel-config-exchange.md), [17](17-agent-wireguard-tunnel.md), [25](25-tunnel-status-freshness.md).

## 작업 범위
- 포함
  - 플랫폼 API: `TUNNEL_EXPOSE_KUBE_API`(기본 `false`)·`TUNNEL_KUBE_API_PORT`(기본 6443) 설정. 켜면 `GET /agents/{id}/tunnel/config`의 `exposed_ports`와 새 필드 `kube_api_port`, 중계 서버 목록 `allowed_ports`에 들어간다(폐기 Agent는 빈 목록 그대로)
  - Agent: `kube_api_port`를 `kubernetes.default.svc:443`으로 TCP 전달, 터널 경유 TCP 연결 확인, 보고 `kube_api_ok`
  - Chart: `tunnel.exposeKubeApi`(기본 `false`) → `--expose-kube-api`
  - 상태 저장: `agent_tunnel_status.kube_api_ok`(nullable, Alembic `20261107_01`), `tunnel_view`에 노출
- 제외
  - 중계 서버 Envoy TCP 전달(4단계, 트래픽 담당), kubeconfig·ServiceAccount 발급(5단계), 데이터 코드, 서버 배포, Agent 이미지 태그·릴리스(승인 필요)

## 착수 결정 (2026-11-07, Issue #299)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| K1 | 켜는 곳 | 플랫폼 설정(포트를 정함)과 Chart 값(클러스터 소유자 동의) **둘 다** 켜야 전달한다. 둘 다 기본 꺼짐 | API 노출은 앱·DB보다 위험이 크다. 플랫폼 설정만으로 고객 클러스터 API가 열리면 안 된다 |
| K2 | 포트 전달 계약 | 설정 조회에 `kube_api_port: int \| null` 필드를 더하고 `exposed_ports`에도 넣는다 | 정수 목록만으로는 Agent가 어느 포트가 API인지 모른다. 중계 서버는 `allowed_ports`만 보면 된다(계약 그대로) |
| K3 | 전달 대상 | `kubernetes.default.svc:443` (DNS, TCP만) | Agent는 TLS를 풀지 않고 중계만 한다. Service·Endpoints 조회가 없어 **RBAC 추가 없음** |
| K4 | 기존 규칙 | 터널 IP에만 bind, 출발지 `10.200.0.1`(+자기 터널 IP)만, 로그·보고는 코드만 | 17 W2·W3·W8 그대로. 같은 `TcpForwarders`를 쓴다 |
| K5 | 앱 포트와 겹침 | `kube_api_port`는 앱 Service 탐색에서 뺀다(Agent가 꺼도). 앱이 같은 포트를 쓰면 API가 이긴다 | 6443 앱은 데모에 없다. 운영자는 `TUNNEL_KUBE_API_PORT`를 바꾼다 |
| K6 | 확인·보고 | 매 주기 터널 IP:포트 TCP 연결 확인 → `kube_api_ok`. **`code`·`app_health_ok`·`db_connect_ok`·인계 판정을 바꾸지 않는다.** 노출 안 할 때는 필드를 보내지 않는다 | API 확인 실패로 트래픽 인계·66 빠른 재확인이 흔들리면 안 된다. 예전 플랫폼(`extra=forbid`)과 호환 |
| K7 | 잘못된 포트 | 정수가 아니거나 1~65535 밖, 5432, bool은 무시(노출 안 함) | 설정 오류로 터널 전체를 실패시키지 않는다 |
| K8 | TLS | k3s 서버 인증서 SAN에 터널 IP가 없다. kubeconfig는 `server: https://<중계 서버 주소>:<포트>`와 `tls-server-name: kubernetes.default.svc`(SAN에 있음)를 쓴다. `insecure-skip-tls-verify`는 쓰지 않는다 | 5단계 kubeconfig 발급 때 반영 |

## 공개 인터페이스

```text
GET /agents/{id}/tunnel/config  → { ..., "exposed_ports": [5432, 6443, 8080], "kube_api_port": 6443 | null }
POST /agents/{id}/tunnel/status ← { ..., "kube_api_ok": true | false }   # 선택, 노출할 때만
GET /relays/{id}/agents         → allowed_ports에 6443 포함(켰을 때)
```

```python
# cmp_providers.agent_tunnel
KUBE_API_HOST = "kubernetes.default.svc"; KUBE_API_TARGET_PORT = 443
kube_api_port(config: dict) -> int | None
tcp_connect_ok(ip: str, port: int, timeout: float = 5) -> bool
TunnelManager(..., tcp_ok=tcp_connect_ok, expose_kube_api=False)
# python -m cmp_providers.agent_tunnel --expose-kube-api
```

## 수용 기준
- [x] 기본(설정 꺼짐)은 `kube_api_port: null`, 노출 포트·중계 서버 목록이 예전과 같다
- [x] 켜면 Agent 설정·중계 서버 목록에 포트가 들어가고, 폐기 Agent는 빈 목록이다
- [x] Agent는 Chart 값이 켜졌을 때만 그 포트를 `kubernetes.default.svc:443`으로 전달하고, 앱 Service 탐색에 넣지 않는다
- [x] 전달기는 터널 IP에만, 중계 서버 출발지만 받는다(기존 `TcpForwarders`)
- [x] `kube_api_ok`는 따로 저장되고 `code`·인계 판정을 바꾸지 않는다. 노출 안 하면 보고에 없다
- [x] Chart 값 검증, RBAC 변화 없음
- [ ] 실측: 중계 서버(4단계) 경유 `kubectl --kubeconfig … get --raw /readyz` — 4·5단계 뒤

## 시험
`tests/test_tunnel_exchange.py`(설정·중계 서버 목록·저장·마이그레이션), `tests/test_agent_tunnel.py`(전달 대상·확인·옛 형식·잘못된 포트·Chart 동의), `tests/test_onprem_agent_chart.py`(인자·검증·RBAC 동일). `make check`, `make openapi`.

## 남은 일
- Agent 이미지 릴리스(이 변경을 담은 태그) — 승인 필요
- 중계 서버 TCP 전달(4단계, 트래픽 담당): 중계 서버 주소의 포트 → `터널IP:6443`, worker 출발지만
- kubeconfig·ServiceAccount 발급(5단계): K8 TLS 설정 포함 → [75](75-data-migration-kubeconfig.md)
