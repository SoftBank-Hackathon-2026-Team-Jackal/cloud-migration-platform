# 온프레미스 데이터 터널 2안 실측 (티켓 69)

원본 EKS(`cmp-demo-source`, 서울) ↔ 맥 k3d(`k3d-cmp-local`, arm64, NAT 뒤). 입구는 원본 EKS UDP NLB, k3d가 밖으로 접속한다.
구성·실행: [infra/data-migration/onprem-poc](../../infra/data-migration/onprem-poc/README.md). 비밀값·키·공인 IP는 이 문서에 적지 않는다. 방식 결정: [ADR-007](../decisions/ADR-007.md).

## 결과 요약 (2026-11-07 20:40~20:58Z, 실제 실행)

| 질문 | 판정 | 증거 |
| --- | --- | --- |
| Q1 원본 EKS UDP NLB(허용 CIDR) 생성·주소 | **PASS** | LBC가 NLB를 만들고 1초 안에 DNS 이름이 Service에 나왔다. 대상(IP, TCP 8080 헬스체크)이 약 2분 30초 뒤 `healthy`. 허용 CIDR은 맥 공인 IP `/32` 하나 |
| Q2 NAT 뒤 handshake·keepalive 25 유지 | **PASS(약 9분, 조건부)** | 재시작 뒤 즉시 handshake. 약 9분 동안 마지막 handshake가 항상 2분 이내(측정 25초·81초), ping 3/3, 터널 RTT 평균 13ms(9.6~16.8). 양쪽 커널 WireGuard(EKS AL2023, 맥 linuxkit). 원본 쪽 peer 주소가 맥 공인 IP와 같다(UDP IP 대상은 클라이언트 IP 보존, CNI SNAT 문제 없음). **처음 시도는 실패**(아래 주의점 1). 사용자 판단으로 10분 전에 멈췄다: 데모 한 번이 5분이라 그보다 긴 유지는 필요 없다 |
| Q3 터널 경유 원본 DB 5432 `SELECT 1` | **PASS** | k3d Pod → `src-db:5432` → 터널 → 원본 `chat-db-0`. `pg_isready` 응답, `SELECT 1` → 1, `transaction_read_only` → on, 새 연결+질의 91~102ms(3회). 원본 DB 쓰기 없음 |
| 정리 | **PASS** | k3d ns 삭제 → Service 삭제로 LBC가 NLB·대상 그룹·SG 삭제 → AWS에서 NLB·대상 그룹(`cmpdmpoc`)·SG(`service.k8s.aws/stack` 태그)·ELB ENI 없음 확인 → EKS ns 삭제. 원본 `chat-source` Pod는 그대로 |

비용: NLB와 공인 IPv4 2개를 약 17분 썼다(1달러 미만).

## 주의점 (2·3·6단계에 반영할 것)

1. **NLB cross-zone이 꺼져 있으면 DNS의 다른 AZ 주소로 붙어 handshake가 안 된다.** 대상이 healthy가 된 직후 DNS는 두 AZ(2a·2c) 주소를 다 돌려줬고, k3d 게이트웨이는 대상 Pod가 없는 2c 주소를 골랐다(받은 바이트 0). 몇 분 뒤 DNS가 2a 주소만 돌려줄 때 게이트웨이를 다시 시작하니 바로 handshake가 됐다. WireGuard는 endpoint 이름을 한 번만 풀기 때문에 저절로 고쳐지지 않는다. 제품에는 `service.beta.kubernetes.io/aws-load-balancer-attributes: load_balancing.cross_zone.enabled=true`(실측 안 함)를 넣거나, handshake가 시간 안에 되지 않으면 다시 풀고 재접속하는 처리가 필요하다.
2. in-tree 방식인 `aws-load-balancer-type: nlb` 대신 LBC 방식(`loadBalancerClass: service.k8s.aws/nlb`)을 쓴다. LBC 기본 scheme은 internal이라 `internet-facing`을 명시해야 한다. UDP 대상 그룹은 UDP 헬스체크가 없어 TCP 헬스 포트를 따로 둬야 한다.
3. 허용 CIDR은 온프레미스의 **실제 송신 공인 IP**여야 한다. 이번에는 맥의 `checkip` 값과 원본이 본 주소가 같았다. 통신사 NAT처럼 이 값이 바뀌는 환경에서는 접속 직전에 다시 확인해야 한다.
4. NLB 생성부터 대상이 healthy가 될 때까지 약 2분 30초가 걸린다. 데모 시간 안에 이 시간을 넣을지, 미리 만들어 둘지 정해야 한다.
5. PoC 스크립트의 한계: `check.sh` 출력을 파이프로 받으면 관찰 로그가 버퍼에 남아 중간에 멈출 때 잃는다(이번에 실제로 잃었다. Q3·Q2 증거는 따로 다시 측정했다). 단계 2부터는 제품 Activity(`data_tunnel`)의 handshake 확인을 쓴다.

## 사전 조회(읽기만)

AWS Load Balancer Controller v3.6.2가 있다(in-tree 아님, Service 변형 webhook). 공개 서브넷 2개에 `kubernetes.io/role/elb=1` 태그가 있다.
원본 DB `chat-source/chat-db-0`에는 Ingress NetworkPolicy가 없어 다른 Namespace의 게이트웨이가 5432로 붙을 수 있다. 자세한 표는 [티켓 69](../../tickets/provisioning/69-onprem-data-tunnel-poc.md).
