# Migration Network Preflight — 기능 명세

## 상태와 목적

2026-11-05 사용자 합의안. 구현 전 명세이며 실행 완료의 증거가 아니다. AWS EKS 원본의 PostgreSQL 데이터를 온프레미스 K3s로 이전하기 **직전**, 실제 이전 경로의 연결·전송 문제를 발견하고 초기 복사·CDC 시작 여부를 판정한다.

## 범위와 책임

- 대상 경로는 `AWS 원본 PostgreSQL → 온프레미스 K3s`다. 검사 클라이언트는 실제 데이터 이전 작업과 동등한 네트워크 위치에서 원본 DB로 접속한다.
- 공통 모듈은 입력·결과 계약, 네 가지 검사와 통합 판정을 제공한다. Data Migration Worker가 공개 진입점을 한 번 호출해 시작을 차단하거나 진행한다. 온프레미스 Agent는 허용된 전용 검사 작업을 수행한다.
- Provisioning은 원본/대상 네트워크·DB Endpoint·Agent 접근 참조를 제공한다. Traffic Migration은 결과를 참고하되 사용자→앱 경로와 앱 헬스는 자체 검증한다.
- WireGuard 구축·네트워크 설정 자동 수정, PostgreSQL publication/subscription/slot 구성, CDC 중 지연·WAL 누적 감시, 데이터·트래픽 이전 자체는 제외한다.

## 요구사항

### MNP-001 — 정확한 대상과 실행 위치

호출자는 `migration_id`, 실제 이전에 사용할 `source_db_host`/`port`, TLS 설정·인증 참조, AWS/온프레미스에서 **실제로 라우팅되는** CIDR, 터널 CIDR과 기대 사설 경로, `agent_id`·`cluster_uid`·Namespace·복제 실행 위치, 제한 시간·전송량을 제공한다. PgBouncer 또는 PostgreSQL 직접 접속 여부는 주소 입력으로 결정한다. 다른 주소나 인터넷 우회 경로에서의 성공을 실제 복제 경로 성공으로 표시하지 않는다. Secret 원문은 Workflow 입력·결과·로그에 남기지 않는다.

### MNP-002 — `check_cidr_overlap` (정적 검사)

AWS VPC 대역과 온프레미스의 라우팅 대상 Node·Pod·터널 대역에서 충돌을 찾는다. AWS VPC에 속한 정상적인 Subnet 포함 관계와 외부로 라우팅되지 않는 Kubernetes Service 대역은 무조건 충돌로 취급하지 않는다. 충돌한 대역·역할을 반환하고 필수 대역 정보가 없으면 `UNKNOWN`을 반환한다.

### MNP-003 — `check_route` (대상 조회)

복제 실행 위치와 필요한 터널 게이트웨이에서 원본 DB의 해석된 IP까지 선택되는 경로·출발지 IP·인터페이스를 기록한다. 해석된 IP가 허용된 사설 목적지 밖이거나 기대 터널 경로를 벗어나면 `FAIL`이다. 라우트가 없으면 `FAIL`, 실행 위치에 접근하지 못하거나 기대 경로를 확인할 수 없으면 `UNKNOWN`이다. 이 조회만으로 AWS 반환 경로나 방화벽 통과를 증명하지 않는다.

### MNP-004 — `probe_db_path` (실제 연결·방화벽 통과 여부)

실제 Endpoint에서 DNS → TCP → 구성된 TLS 방식 → 인증 → 읽기 전용 `SELECT 1`을 검사한다. DNS·TCP·TLS·인증 오류를 구분해 반환한다. 성공은 일반 SQL 연결의 증거이며 PostgreSQL 복제 권한·프로토콜·slot의 성공 증거가 아니다. 네트워크 실패만으로 특정 방화벽 규칙을 원인으로 단정하지 않는다.

### MNP-005 — `probe_large_transfer` (실제 전송·MTU 진단)

같은 DB 경로에서 읽기 전용 쿼리로 기본 4 MiB의 응답을 스트리밍해 수신 바이트·소요 시간·중단 여부를 기록한다. 전송량·시간·재시도는 유한한 상한을 가진다. 64 KiB 이후 끊김도 실패로 검출한다. 가능한 경우 경로 MTU/DF 진단을 첨부하지만 ICMP 차단만으로 실패로 판정하지 않는다. 전송 실패의 원인을 MTU·방화벽·비대칭 라우팅 중 하나로 자동 확정하지 않는다.

### MNP-006 — `run_network_preflight` (공개 진입점·차단)

각 검사에 `PASS | FAIL | UNKNOWN`과 원인 코드·정제된 증거를 부여하고 전체 판정에 검사 시각·검사 Endpoint·실행 위치를 포함한다. MNP-002~005의 필수 검사가 모두 `PASS`일 때만 전체 `PASS`다. 필수 검사 `FAIL` 또는 `UNKNOWN`은 초기 복사·CDC 시작을 차단한다. 터널·라우트·방화벽·DB Endpoint가 바뀐 뒤 다시 시작할 때는 이전 결과를 재사용하지 않고 재검사한다. 검사 성공은 실행 중 CDC 안정성이나 데이터 정합성을 보장하지 않는다.

## 결과 계약

```text
NetworkPreflightRequest
  migration_id
  source_db_host, source_db_port, tls_mode, db_auth_ref
  aws_vpc_cidrs, onprem_routed_cidrs, tunnel_cidrs
  expected_network_path: allowed_private_destination_cidrs, tunnel_gateway_ref
  probe_target: agent_id, cluster_uid, namespace, workload_ref
  transfer_bytes: 기본 4 MiB
  timeout

NetworkPreflightResult
  status: PASS | FAIL | UNKNOWN
  checks: [{name, status, code, evidence}]
  migration_id, tested_endpoint, probe_location, checked_at
```

`evidence`에는 충돌 CIDR, 경로·출발지 IP, 실패 단계, 수신량·시간 등 작은 진단값만 남긴다. 인증정보·쿼리 응답 본문·터널 키·원본 데이터는 기록하지 않는다. 연결 실패는 `TCP_UNREACHABLE` 같은 관측 코드로 반환하며 확인하지 못한 방화벽 규칙 이름을 만들어내지 않는다.

## 수용 기준

1. CIDR 충돌, 라우트 부재, DB 연결·인증 실패, 대량 전송 중단, 검사 불가를 각각 구분하고 필수 실패에서 시작을 차단한다.
2. ICMP만 차단되고 실제 대량 TCP 전송이 성공하면 MTU 보조 진단만으로 실패시키지 않는다.
3. 검사와 결과에는 DB 쓰기·Secret 원문·전송 데이터 본문이 없다.
4. 같은 요청의 재시도는 안전하며 경로 입력 변경 후 이전 `PASS`를 사용하지 않는다.

## 근거

- [Broadcom 453833](https://knowledge.broadcom.com/external/article/453833/tcp-communication-from-onpremises-machin.html): 약 64 KiB 전송 뒤 비대칭 경로·상태 저장 방화벽의 ACK 추적으로 중단된 사례.
- [Broadcom 442569](https://knowledge.broadcom.com/external/article/442569/error-downloading-plugin-url-is-unreacha.html): 오버레이 MTU 병목으로 HTTPS 전송이 실패한 사례.
- [PostgreSQL 16 복제 모니터링](https://www.postgresql.org/docs/16/logical-replication-monitoring.html): 복제 시작 후 감시가 별도로 필요한 근거.
