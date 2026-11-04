# Ticket 16 — 터널 설정 교환 API와 결과 계약

## 상태
TODO — [Issue #70](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/70). 플랫폼 쪽 계약은 이 티켓에서 확정한다. 중계 서버가 읽어 가는 형식은 Traffic 담당(상헌님)과 확인한 뒤 구현한다(아래 결정 대기).

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
