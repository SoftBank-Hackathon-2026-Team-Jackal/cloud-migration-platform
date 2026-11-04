# Ticket 16 — 터널 설정 교환 API와 결과 계약

## 상태
IN_PROGRESS — [Issue #70](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/70). 2026-11-05 사용자 지시로 **Traffic 담당(이상헌님) 답변과 무관한 부분부터 착수**했다. 답변이 필요한 부분은 아래 "⚠️ 답변 대기 — 답변 받은 뒤 진행" 표에 남겼고, 그 항목은 답변 전에는 구현하지 않는다.

## 목적
노트북의 Agent와 AWS 중계 서버가 WireGuard로 연결되려면 서로의 공개키·주소를 알아야 한다. 둘은 서로를 직접 모르므로 플랫폼이 이 값을 전달한다. 연결 결과는 `ProvisioningResult`로 Data/Traffic Migration에 넘긴다.

```text
사용자 → DNS → [중계 서버: Envoy + WireGuard 서버]  ← Traffic 담당
                         ↕ WireGuard 터널
               [노트북: Agent + WireGuard 클라이언트 + K3s 앱/DB]  ← Provisioning(15·17)
Agent ─HTTPS─→ 플랫폼 API ←─HTTPS─ 중계 서버   (이 티켓: 키·설정·상태 교환)
```

## 관련 요구사항
PROV-009, PROV-019, PROV-020, PROV-021

## 선행 작업
Blocked by: 13 (DONE), 15 (Agent 장기 실행 기반; API 자체는 병행 가능)

## 작업 범위
- 포함
  - Agent → 플랫폼: WireGuard 공개키 등록·교체. 비밀키는 받지 않는다.
  - 플랫폼 → Agent: 터널 설정 조회(중계 서버 목록 `[{endpoint, public_key}]`, 배정 터널 IP, AllowedIPs, MTU 1420, PersistentKeepalive, 터널에 노출할 포트 목록, 설정 버전).
  - 중계 서버 등록(운영자): 중계 서버 ID·WireGuard 공개 endpoint·공개키·중계 서버 전용 자격 증명.
  - 플랫폼 → 중계 서버: 인증된 중계 서버가 연결할 Agent 목록 조회(`agent_id`, 공개키, 터널 IP, 허용 포트, 버전).
  - 터널 IP 배정: 설정된 터널 CIDR에서 Agent마다 /32를 배정하고 재등록·키 교체에도 유지.
  - Agent → 플랫폼: 터널 상태 보고(마지막 handshake 시각, 터널 IP, 터널 경유 앱 헬스·DB 접속 확인 결과).
  - 결과 계약: ON_PREM `external_endpoint`에 터널 쪽 앱 목적지를 채우고(T1), DB 터널 목적지와 Data 인계 조건을 추가한다.
  - OpenAPI 재생성, `shared-contracts.md`·`onprem-shared-deployment.md`·13 티켓 갱신.
- 제외: 중계 서버 구현(Envoy 설정, WireGuard 서버 peer 반영 방식), DNS, 사용자 공인 주소, TLS, 노트북 쪽 WireGuard 실행(17).

## 수용 기준
- [ ] 등록된 Agent만 자기 공개키를 등록·교체하고 자기 터널 설정만 조회한다. 다른 Agent·소유자 정보는 보이지 않는다.
- [ ] 인증된 중계 서버만 Agent 목록을 조회하고, 폐기된 Agent(15)는 목록에서 빠진다.
- [ ] 같은 Agent의 터널 IP는 재조회·키 교체 후에도 같고, Agent 간 중복이 없다.
- [ ] 중계 서버 공개키·endpoint가 바뀌면 설정 버전이 올라가 Agent가 변경을 알 수 있다.
- [ ] ON_PREM 결과의 `internal_endpoint`는 클러스터 내부 주소, `external_endpoint`는 터널 쪽 목적지(`http://터널IP:포트`)이며 중계 서버 공인 주소는 어느 필드에도 넣지 않는다.
- [ ] 터널 경유 앱 헬스가 확인된 ON_PREM 결과에만 `external_endpoint`가 채워져 Traffic 인계가 진행되고, 터널 경유 DB 접속이 확인된 결과만 Data 인계가 진행된다. 확인 전에는 차단 코드가 남는다.
- [ ] 비밀키·자격 증명 원문이 DB·작업·로그·Temporal 이력에 없다.

## Test Seam
- FastAPI `TestClient` + SQLite 업무 DB: 공개키 등록, 설정 조회, 중계 서버 등록·목록 조회, 상태 보고, 인증·격리·폐기.
- `get_migration_handoff`와 결과 API: 터널 확인 전·후의 ON_PREM Traffic/Data 진행 여부와 노출 범위.
- 놓치는 것: 실제 WireGuard 연결(17), 실제 중계 서버의 목록 반영(Traffic).

## 착수 결정 후보 (구현 시작 시 확정)
| ID | 후보 | 근거 |
| --- | --- | --- |
| T1 결과 필드 (**확정 2026-11-05**) | `ApplicationHandoff.external_endpoint`의 의미를 "중계 서버(Envoy)가 요청을 전달할 목적지"로 정의한다. AWS/GCP는 지금처럼 LB 주소, ON_PREM은 터널 경유 확인 후 `http://터널IP:앱포트`. 중계 서버 공인 주소는 넣지 않는다(Traffic 소유). DB 터널 목적지는 `DatabaseHandoff`에 별도 필드(`tunnel_endpoint`)로 추가 | 사용자 결정. Traffic 티켓 #61·#63이 이미 `external_endpoint`를 전달 목적지로 읽으므로 그쪽 변경이 필요 없다 |
| T2 ON_PREM 인계 조건 | Traffic: 13 R7 그대로(`READY` + `application.ready` + `external_endpoint` 존재). ON_PREM은 터널 경유 앱 헬스가 확인될 때만 `external_endpoint`를 채운다. Data: `READY` + `database.ready` + 터널 경유 DB 접속 확인. AWS/GCP 조건은 그대로 | R7 조건을 바꾸지 않고 ON_PREM 차단을 푼다 |
| T3 중계 서버 인증 | 운영자가 등록할 때 발급하는 중계 서버 전용 자격 증명(Agent 자격 증명과 같은 방식, 원문 1회 반환) | 04A 방식 재사용 |
| T4 터널 CIDR | 설정값(기본 후보 `10.200.0.0/24`, 중계 서버 `.1`). AWS VPC·온프레미스 CIDR과 겹치는지는 #51 Preflight가 검사 | 실측 구성과 같은 단일 /24로 충분 |
| T5 노출 포트 | 앱 포트와 PostgreSQL 5432. 원천은 승인 Plan의 앱 포트 | 데이터 이전도 터널로 원본 DB → 온프레미스 DB 복제를 한다 |

## 착수 결정 (2026-11-05, 답변 무관 부분)

T1은 사용자 결정으로 확정됐다. T2·T3·T5는 아래처럼 확정하고, T4는 P3 답변 전 기본값, 새 결정 T6·T7을 더한다.

| ID | 결정 |
| --- | --- |
| T2 확정 | ON_PREM Data 인계는 `READY` + `database.ready` + **터널 경유 DB 접속 확인**일 때만 진행한다(아니면 `TUNNEL_DB_NOT_VERIFIED`). Traffic 인계 조건은 13 R7 그대로이며, ON_PREM `external_endpoint`를 채우는 일은 P2 답변 후 한다. AWS/GCP 조건은 바뀌지 않는다. |
| T3 확정 | 중계 서버는 운영자(관리자 키)가 `relay_id`·WireGuard 공개 endpoint·공개키로 등록하고, 플랫폼이 중계 서버 전용 자격 증명을 한 번만 원문으로 돌려준다(DB에는 digest만). 같은 `relay_id`를 다시 등록하면 endpoint·공개키를 갱신하고 자격 증명은 유지한다. |
| T4 기본값 | 설정 `TUNNEL_CIDR`(기본 `10.200.0.0/24`), 중계 서버 주소 `.1` 예약, Agent는 `.2`부터 빈 /32를 배정하고 재등록·키 교체에도 유지한다(P3 답변으로 바뀌면 설정만 변경). |
| T5 확정 | 터널에 노출할 포트 = 그 Agent의 READY ON_PREM 결과에 있는 앱 포트들 + PostgreSQL 5432. |
| T6 결과 불변 유지 | 13 R3의 결과 불변을 지킨다. 터널 확인은 결과 저장 뒤에 오므로, `get_migration_handoff`가 **읽는 시점에** 그 결과의 `cluster.agent_id`에 대한 최신 터널 상태를 합쳐 ON_PREM 인계 뷰를 만든다. 저장된 결과는 바꾸지 않는다. |
| T7 DB 터널 목적지 | `DatabaseHandoff.tunnel_endpoint`(기본 `null`)를 추가한다. ON_PREM이고 터널 상태가 DB 접속 성공을 보고했을 때만 인계 뷰에 `터널IP:5432`로 채운다. |
| T8 설정 버전 | 중계 서버 등록·갱신마다 1씩 오르는 정수 `config_version`. Agent 설정 응답과 키 등록 응답에 담아 Agent가 변경을 알 수 있게 한다. |
| T9 공개키만 | WireGuard 공개키(Base64 32바이트) 형식만 받는다. 비밀키는 어떤 API에도 없다. 폐기된 Agent(15)는 인증 단계에서 거절된다. |

## ⚠️ 답변 대기 — 답변 받은 뒤 진행 (2026-11-05 기록)

상헌님께 확인 메시지를 보냈다(사용자 전달). **아래 항목은 답변을 받은 뒤에만 진행한다.** 답변을 받으면 이 표의 "답변"을 채우고 해당 작업을 끝낸 뒤 DONE을 검토한다.

| # | 확인할 것 | 현재 가정 | 답변 전 처리 | 답변 후 할 일 | 답변 |
| --- | --- | --- | --- | --- | --- |
| P1 | 중계 서버가 Agent 목록을 받는 방식(주기 폴링 / push / 파일)과 응답 필드 | 플랫폼 조회 API 주기 폴링, 필드 `agent_id`·공개키·터널 IP(/32)·허용 포트·설정 버전, 폐기 Agent 제외 | **미구현.** 중계 서버 등록·인증(T3)까지만 구현 | 확정된 방식·필드로 중계 서버용 목록 API 구현·테스트, OpenAPI 갱신 | (대기) |
| P2 | 온프레미스 `external_endpoint` 포트: 적힌 포트 그대로 사용인지, 80 고정인지(#63 수용 기준 "포트 80") | `http://터널IP:앱포트`(예: `:8080`) | **미구현.** ON_PREM Traffic 인계는 계속 `EXTERNAL_ENDPOINT_UNAVAILABLE`로 차단 | 앱 포트면 터널 경유 앱 헬스 확인 후 `external_endpoint` 채우기. 80 고정이면 Agent 쪽 80→앱포트 포워딩(17)도 함께 결정 | (대기) |
| P3 | 터널 대역과 중계 서버 주소 배정 주체 | `10.200.0.0/24`, 중계 서버 `.1`, Agent별 /32 플랫폼 배정 | 기본값으로 구현(설정값) | 다르면 설정값만 변경 | (대기) |
| P4 | 중계 서버 EC2(인스턴스·EIP·보안 그룹) 생성 주체 | Traffic 담당이 만들고 운영자가 플랫폼에 endpoint·공개키 등록 | 코드 영향 없음 | Provisioning이 만들어야 하면 별도 티켓으로 분리 | (대기) |
| P5 | `external_endpoint` = "중계 서버가 전달할 목적지"(T1) 공유 확인 | #61·#63이 같은 의미로 읽음 | T1대로 진행 | 다르게 읽고 있으면 T1 재검토 | (대기) |

## 결정 대기 (Traffic 담당과 확인)
1. 중계 서버가 Agent 목록을 읽는 방식: 위 조회 API를 주기 폴링할지, 다른 형식이 필요한지.
2. 터널 CIDR과 중계 서버 쪽 주소 배정 주체.
3. 중계 서버 EC2 자체(인스턴스·EIP·보안 그룹)의 Terraform 생성 주체.
4. ~~T1과 Traffic 티켓의 필드 충돌~~ → 2026-11-05 사용자 결정으로 `external_endpoint` 사용(T1). 이 정의를 #61·#63 담당자에게 공유한다.

## 완료 시 확인할 동작
Agent와 중계 서버가 플랫폼을 통해 서로의 공개키·주소를 얻고, 터널 확인 결과가 Data/Traffic 인계 조건에 반영된다.

## 관련 문서
- [13](13-result-handoff.md) · [15](15-agent-runtime-install.md) · [17](17-agent-wireguard-tunnel.md) · [공유 계약](../../specs/provisioning/shared-contracts.md)
- [도메인 경계](../../docs/architecture/domain-boundaries.md) · [Migration Network Preflight](../../specs/migration-network-preflight/spec.md)

## 설계 변경 기록
- 2026-11-05 사용자 결정: 중계 서버안(Route 53 → Envoy 중계 → WireGuard → 온프레미스)을 전제로 한다. Provisioning은 Agent 쪽 연결과 플랫폼 교환 API·결과 계약까지, 중계 서버 내부는 Traffic 담당이다. PROV-009의 "Data/Traffic이 사용할 대상 접근 참조 제공"을 터널 목적지로 구체화한다. 같은 날 `external_endpoint`를 "중계 서버가 전달할 목적지"로 정의하기로 결정했다(T1). 13·shared-contracts의 필드 설명과 spec 문구 갱신은 구현 PR에 포함한다.
- 참고: 상헌님 실측(11/3~4) — 서울 AWS 중계 ↔ 춘천 서버 WireGuard 왕복 4.5ms, MTU 1420 필수, 양쪽 PersistentKeepalive 권장.

## 완료 체크리스트
- [ ] 수용 기준과 Test Seam 확인
- [ ] 관련 테스트 통과와 회귀 확인
- [ ] 설계 변경 여부 확인 및 필요한 SDD·ADR·티켓 반영
- [ ] 구현·계약 정합성 및 최종 리뷰
- [ ] 미검증 항목과 결과 증거 기록

## 구현 (2026-11-05, 답변 무관 부분)

- 업무 DB `cmp_api.tunnels`(Alembic `20261105_03`): `agent_tunnels`(공개키·터널 IP, 재등록·키 교체에도 같은 IP), `relay_servers`(endpoint·공개키·자격 증명 digest), `tunnel_settings`(설정 버전), `agent_tunnel_status`(최신 터널 상태).
- API(`cmp_api.routes.tunnels`, Agent는 기존 Bearer 자격 증명·폐기 Agent 거절, 실패는 기존 규칙대로 403):
  - `PUT /agents/{agent_id}/tunnel/key` — WireGuard 공개키(Base64 32바이트)만 받음, 알 수 없는 필드(예: `private_key`) 거절 → `tunnel_ip`·`config_version`
  - `GET /agents/{agent_id}/tunnel/config` — 중계 서버 목록 `[{relay_id, endpoint, public_key}]`, 터널 IP, AllowedIPs(터널 대역), MTU 1420, PersistentKeepalive 25, 노출 포트(5432 + READY ON_PREM 앱 포트), 설정 버전. 키 미등록이면 404 `TUNNEL_KEY_NOT_REGISTERED`
  - `POST /agents/{agent_id}/tunnel/status` — 마지막 handshake 시각, 터널 경유 앱 헬스·DB 접속 결과. 다른 터널 IP는 409 `TUNNEL_IP_MISMATCH`
  - `PUT /tunnel-admin/relays/{relay_id}` (운영자 키) — 첫 등록 201 + 자격 증명 원문 1회, 재등록 200(자격 증명 미표시), 등록·갱신마다 설정 버전 +1
- 인계(T2·T6·T7): `get_migration_handoff`가 ON_PREM 결과를 읽을 때 그 Agent의 최신 터널 상태를 합친다. Data는 터널 경유 DB 접속 성공 보고 뒤에만 진행하고 `database.tunnel_endpoint = 터널IP:5432`를 채운다. 그 전에는 `TUNNEL_DB_NOT_VERIFIED`. 저장된 결과는 바꾸지 않는다. Traffic은 P2 답변 전이라 계속 `EXTERNAL_ENDPOINT_UNAVAILABLE`.
- 13 변경: `DatabaseHandoff.tunnel_endpoint` 추가, ON_PREM Data 인계 조건 강화(13 테스트 기대값을 T2에 맞게 수정).

## 검증 기록 (2026-11-05)

| 명령·환경 | 결과 |
| --- | --- |
| `uv run pytest tests/test_tunnel_exchange.py` | 12개 통과: 터널 IP 배정·중복 없음·키 교체 후 유지, 공개키 형식·`private_key` 거절, 다른 Agent·폐기 Agent 거절, 중계 서버 자격 증명 1회 반환·digest 저장·재등록 시 버전 증가, 운영자 키 필요, 설정 응답(중계 서버·MTU·Keepalive·노출 포트·소유자/자격 증명 비노출), 키 미등록 404, 터널 DB 확인 전후 Data 인계·결과 불변, Traffic 차단 유지(P2), READY 앱 포트 노출, 터널 IP 불일치 409, Alembic 모델 일치 |
| `UV_CACHE_DIR=/tmp/cmp-uv-cache make check` | Ruff·전체 753개 통과(분석 쪽 로컬 전용 1개 skip) |
| `make openapi` | 터널 경로 4개와 스키마 7개만 추가, 기존 경로 변경 없음, `private` 필드 없음 |

### 남은 작업 (답변 대기 — 위 "⚠️ 답변 대기" 표)

- **P1** 중계 서버용 Agent 목록 API: 미구현. 중계 서버 인증 함수(`authenticate_relay`)만 준비했다.
- **P2** ON_PREM `external_endpoint`(포트): 미구현. Traffic 인계 차단 유지.
- P3·P4·P5: 기본값으로 진행했고 답변에 따라 설정·문서만 바꾼다.
- 실제 WireGuard 연결과 터널 경유 헬스·DB 확인을 보고하는 Agent 쪽 구현은 17이다.

