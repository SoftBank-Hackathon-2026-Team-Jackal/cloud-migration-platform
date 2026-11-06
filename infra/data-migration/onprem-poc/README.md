# 온프레미스 데이터 터널 2안 실측 PoC (티켓 69, 일회용)

결선 데모(원본 EKS → 맥 k3d, NAT 뒤)의 데이터 복제 경로를 확인한다. `cmp_data.tunnel`(입구 = 대상 LB)의 방향을 뒤집어
**입구를 원본 EKS의 UDP NLB**에 두고, NAT 뒤 k3d 게이트웨이가 밖으로 접속한다. 데이터·Agent·중계 코드는 바꾸지 않는다.

```
원본 EKS cmp-demo-source (ns cmp-dm-poc)                    맥 k3d k3d-cmp-local (ns cmp-dm-poc)
 chat-db:5432 ◀─ socat ◀─ gw 10.211.0.1 ◀─ NLB UDP 51820 ◀── gw 10.211.0.2 (endpoint=NLB, keepalive 25)
                          TCP 8080(NLB 헬스체크)   (맥 공인 IP /32만)   Service src-db:5432 → 터널 → 원본 chat-db
```

- 터널 대역 `10.211.0.0/24`(데이터 10.210, 중계 10.200과 겹치지 않게), MTU 1420, 이미지 `alpine:3.22`(amd64·arm64).
- NLB는 AWS Load Balancer Controller(`loadBalancerClass: service.k8s.aws/nlb`, internet-facing, IP 대상)가 만든다.
  UDP는 헬스체크가 안 되어 게이트웨이 TCP 8080으로 본다.
- WireGuard 비밀키는 로컬에서 만들어 stdin으로 각 클러스터 Secret `wg-key`에만 넣는다. 화면·인자·저장소에 없다.
  원본 DB 비밀번호는 `check.sh`가 원본 Secret → k3d Secret으로 파이프로만 옮기고 확인 뒤 지운다.
- 원본 DB에는 `default_transaction_read_only=on`으로 `SELECT 1`만 보낸다.

## 실행 (사용자 승인 뒤)

```bash
CONFIRM_AWS=yes infra/data-migration/onprem-poc/up.sh     # 원본 EKS·AWS 변경(NLB 생성)
infra/data-migration/onprem-poc/check.sh                  # Q1~Q3 확인, 기본 11분 관찰
infra/data-migration/onprem-poc/down.sh                   # NLB·대상 그룹·SG 삭제 확인 후 양쪽 ns 삭제
```

`AWS_PROFILE`(기본 `cmp-aws-prod`), `EKS_CONTEXT`, `K3D_CONTEXT`(기본 `k3d-cmp-local`), `SOURCE_CIDR`(기본 지금 맥 공인 IP /32).
매니페스트만 보려면 `python3 poc.py eks --peer-public-key <pub> --source-cidr <ip>/32`.

결과: [docs/engineering/onprem-data-tunnel-poc.md](../../../docs/engineering/onprem-data-tunnel-poc.md)
