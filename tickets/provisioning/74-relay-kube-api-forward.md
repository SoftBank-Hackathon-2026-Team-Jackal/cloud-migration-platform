# Ticket 74 — 중계 서버 K3s API TCP 전달 (ADR-007 2안 4단계, 트래픽 영역 대행)

## 상태
IN_PROGRESS (로컬 구현·시험·Envoy v1.32 로드 확인 완료, 리뷰 대기. 중계 서버 반영·보안 그룹 갱신·실측 남음) — [Issue #303](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/303). 2026-11-07 조율 세션 지시로 착수.

> **트래픽 영역 대행 — 이상헌님 공유 필요.** 중계 서버(Envoy·peer 동기화)는 트래픽 담당 영역이다. 사용자가 Provisioning(김민서) 대행을 결정했다. 사용자 HTTP 경로(Envoy `envoy`, 8080·9901, `cluster_origin/target`, RDS·CDS 파일)는 바꾸지 않았다.

## 배경
[ADR-007](../../docs/decisions/ADR-007.md) 2안에서 데이터 이전 worker(데모 서버 `cmp-demo-control-plane`, 172.31.15.223)는 kubeconfig로 맥 k3d API에 붙는다. [73](73-agent-kube-api-port.md)에서 Agent가 터널 IP:`kube_api_port`(기본 6443)를 `kubernetes.default.svc:443`으로 전달하고 출발지 10.200.0.1(중계 서버)만 받는다. 남은 고리는 같은 VPC의 중계 서버(`cmp-demo-relay`, 172.31.2.84)가 worker 연결을 그 포트로 넘기는 것이다.

## 관련 요구사항
PROV-009, 017, 021, DM-011, ADR-007, [16](16-tunnel-config-exchange.md), [63 peer 동기화](../../packages/traffic/src/cmp_traffic/peers.py), [70](70-relay-stream-routes.md), [73](73-agent-kube-api-port.md).

## 작업 범위
- 포함
  - 플랫폼 API: `GET /relays/{relay_id}/agents` 항목에 `kube_api_port: int | null`(꺼짐·폐기 = null) + OpenAPI
  - 중계 서버: `cmp_traffic.kube_forward`(LDS·CDS 파일 생성), peer 동기화에 연결(`KUBE_FORWARD_BIND_IP`를 줄 때만)
  - `infra/relay/enable-kube-api-forward.sh`(전달 전용 Envoy `envoy-kube` + peer-sync 설정), `relay-stack.yaml` 접근 제어 파라미터(기본 닫힘), `deploy-relay-stack.sh` 선택 인자
- 제외
  - 실제 반영(스택 갱신·서버 배포·스크립트 실행) — 승인 필요. Agent 코드, 데이터 코드, kubeconfig 발급(75), 사용자 HTTP 경로 변경

## 착수 결정 (2026-11-07, Issue #303)

| ID | 항목 | 결정 | 근거·대안 |
| --- | --- | --- | --- |
| R1 | 전달기 | **별도 Envoy**(`envoy-kube`, 호스트 네트워크, 관리 포트 127.0.0.1:9902)가 `/etc/envoy-kube/{lds,cds}.json` 파일을 감시(동적, 파일 방식) | 기존 동적 대상(CDS 파일)과 같은 방식. 사용자 Envoy에 LDS를 더하려면 bootstrap 교체·재시작이 필요하고, 그 CDS 파일은 relay-worker가 `cluster_target` 하나로 통째로 덮어써 함께 쓸 수 없다. 정적 설정은 Agent마다 재시작이 필요해 버림 |
| R2 | 누가 파일을 쓰나 | peer 동기화(`PeerSyncer(forwards=…)`)가 wg peer 적용 **뒤** 같은 목록으로 쓴다. 목록이 잘못되면(중복·범위 밖) 아무 파일도 바꾸지 않는다 | 이미 목록을 폴링·검증하는 상시 프로세스. 새 폴러를 두지 않는다 |
| R3 | 리슨 주소 | 중계 서버 **사설 IP 하나**(`KUBE_FORWARD_BIND_IP`, 스크립트는 IMDSv2 `local-ipv4`). 0.0.0.0·공인·루프백·터널 대역(10.200.0.0/24)은 거부 | 공인 노출 금지. 터널 대역에 열면 Agent 쪽에서 닿는다 |
| R4 | 리슨 포트 | `16400 + (터널 IP − 10.200.0.0)` → 10.200.0.2 = **16402**, 최대 16654. `KUBE_FORWARD_BASE_PORT`로 바꿀 수 있다 | 터널 IP는 키를 바꿔도 유지된다(16 T4). 다른 Agent·순서와 무관하고 75번이 터널 IP만으로 계산한다. "순번" 방식은 Agent 추가·폐기 때 포트가 밀린다 |
| R5 | 대상 | `<Agent 터널 IP>:<kube_api_port>`, 출발지 10.200.0.1 고정(`upstream_bind_config`). TCP 그대로(TLS 안 풂), 헬스 확인 없음 | Agent는 출발지 10.200.0.1만 받는다(73 K4) |
| R6 | 제거 | 폐기(`revoked`), `kube_api_port` null, 그 포트가 `allowed_ports`에 없음 → 리스너·클러스터 없음 | 플랫폼이 끄거나 Agent를 폐기하면 다음 동기화(30초)에 닫힌다 |
| R7 | 계약 | `RelayAgent.kube_api_port: int \| null` 추가(선택 필드). 중계 서버는 필드가 없으면 null로 본다 | 정수 목록만으로는 어느 포트가 API인지 모른다(73 K2와 같은 이유). `list_version`이 한 번 바뀐다(무해) |
| R8 | 접근 제어 | 보안 그룹 `KubeApiForwardSourceSecurityGroupId`(또는 `…SourceCidr` /32), TCP 16402–16654. 둘 다 빈 값이 기본(닫힘). 0.0.0.0/0 규칙 없음 | 데모 서버만. 실제 스택 갱신은 승인 뒤 |

## worker가 쓸 주소 (75번 kubeconfig 규칙)

```yaml
clusters:
- name: onprem-k3d
  cluster:
    server: https://<중계 서버 사설 IP>:<16400 + Agent 터널 IP 마지막 옥텟>   # 데모: https://172.31.2.84:16402
    tls-server-name: kubernetes.default.svc   # k3s 인증서 SAN에 있음(73 K8). insecure-skip-tls-verify 금지
    certificate-authority-data: <k3s CA>
```

- Agent 터널 IP는 `GET /relays/{id}/agents`의 `tunnel_ip`(또는 Agent `GET /agents/{id}/tunnel/config`)에서 읽는다. 터널 대역이 /24가 아니면 `16400 + (터널 IP − 대역 네트워크 주소)`.
- 중계 서버 사설 IP는 중계 서버 설정값(`KUBE_FORWARD_BIND_IP`)이다. 플랫폼 API에 싣지 않았다(데모 단일 중계 서버). 운영 설정으로 worker에 준다.

## 공개 인터페이스

```text
GET /relays/{id}/agents → agents[].kube_api_port: int | null   # 꺼짐·폐기 null
```

```python
# cmp_traffic.kube_forward
DEFAULT_BASE_PORT = 16400; ADMIN_PORT = 9902
listen_port(tunnel_ip, tunnel_cidr, base_port=16400) -> int
check_bind_ip(value, tunnel_cidr) -> str
kube_forwards(listing, tunnel_cidr, base_port) -> list[KubeForward]
render_lds(forwards, bind_ip, version) / render_cds(forwards, tunnel_cidr, version) -> dict
render_kube_bootstrap(lds_path, cds_path, admin_port=9902) -> str
KubeForwardFiles(directory, bind_ip, tunnel_cidr, base_port).apply(listing)
# cmp_traffic.peers: PeerSyncer(..., forwards=None); env KUBE_FORWARD_BIND_IP / _DIR / _BASE_PORT
```

## 수용 기준
- [x] 꺼짐·폐기면 중계 서버 목록 `kube_api_port`가 null, 켜면 포트 값
- [x] Agent마다 사설 IP:결정적 포트 → 터널 IP:`kube_api_port` TCP 프록시, 출발지 10.200.0.1
- [x] 0.0.0.0·공인·루프백·터널 대역 bind 거부, 파일을 쓰기 전에 거부
- [x] 폐기·미노출·허용 포트 밖이면 리스너·클러스터 제거, 잘못된 목록이면 파일 그대로
- [x] 사용자 HTTP 경로 Envoy 설정·코드 변화 없음(별도 Envoy, 9902)
- [x] Envoy v1.32 컨테이너에서 생성 파일 로드·리스너 추가·제거 확인(로컬, 2026-11-07)
- [ ] 실측: 중계 서버 반영 뒤 데모 서버에서 `kubectl --kubeconfig … get --raw /readyz` (75 kubeconfig 뒤, 승인 필요)

## 시험
`tests/traffic/test_traffic_kube_forward.py`(포트 규칙·bind 검사·생성 설정·제거·동기화 연결·환경 설정), `tests/test_tunnel_exchange.py`(중계 서버 목록 필드). `make check`, `make openapi`.

## 남은 일 (승인 필요)
1. 서버 `/opt/cmp/lib` 갱신 뒤 `sudo infra/relay/enable-kube-api-forward.sh`
2. `KUBE_API_FORWARD_SOURCE_SG=<데모 서버 SG> infra/relay/deploy-relay-stack.sh --plan` 검토 → 적용
3. 이상헌님 공유(트래픽 영역 대행)
