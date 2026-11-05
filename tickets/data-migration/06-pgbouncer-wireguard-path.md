# Ticket 06 — 원본 PgBouncer 배치와 WireGuard 복제 경로

## 상태

PgBouncer: DONE. WireGuard: DONE(실습 — EKS ↔ GKE, `infra/data-migration/crosscloud-lab`)

## PgBouncer (DM-010)

- 이미지 `infra/data-migration/cutover`(alpine 3.22, PgBouncer 1.24, psycopg 3, 비 root UID 10001). Pod 하나에 컨테이너 둘: `pgbouncer`, `runner`(전환 실행기)
- `dm_install_cutover`: 원본 앱 Namespace에 Secret `cmp-cutover`(userlist, 연결 대상, 원래 URL)를 만들고 Deployment·Service(`cmp-pgbouncer:6432`)를 server-side apply. 처음 연결 대상은 원본 DB(`local_host`). 서버 쪽 로그인은 DB 컨테이너의 관리자 계정, 앱은 자기 사용자로 붙는다.
- `dm_route_app`: 앱 Secret의 `DATABASE_URL` 호스트·포트만 PgBouncer로 바꾸고 Deployment 재시작(최초 1회, 재실행 시 건너뜀)
- `pool_mode = transaction`, 레플리카 1, `Recreate`
- 앱 재시작 뒤 옛 Pod가 모두 사라질 때까지 기다린다(DM-014). 종료 중인 옛 Pod는 PgBouncer를 거치지 않고 원본 DB에 계속 쓸 수 있다.

실습: 초당 약 50건 쓰기 중 설치·앱 연결 변경에서 오류 0건(앱 재시작은 롤링).

## WireGuard (DM-011)

실습 구성: 각 클러스터에 게이트웨이 Pod(커널 WireGuard, 없으면 wireguard-go)와 `wg-peer-db` Service. GKE 쪽 UDP 입구는 LoadBalancer + EKS NAT IP만 허용. `peer_host`에 상대 클러스터의 `wg-peer-db` 주소를 넣으면 플랫폼 코드는 바꾸지 않아도 된다. EKS → GKE 전환 110ms·롤백 56ms, 누락 0(자세한 값은 crosscloud-lab README).

남은 일:

- 게이트웨이 배치를 플랫폼 Activity로 옮기기(지금은 실습 스크립트 `up.sh`). 게이트웨이는 privileged라 운영 환경 정책 확인 필요.
- 미정: AWS·GCP 대상에서 터널 양 끝 위치(노드, 게이트웨이 Pod, 별도 VM), 터널 생명주기 소유(Provisioning 또는 Data). ON_PREM은 Agent 터널(#71, `tunnel_endpoint`)을 쓴다.
