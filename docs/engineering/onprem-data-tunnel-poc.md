# 온프레미스 데이터 터널 2안 실측 (티켓 69)

원본 EKS(`cmp-demo-source`, 서울) ↔ 맥 k3d(`k3d-cmp-local`, arm64, NAT 뒤). 입구는 원본 EKS UDP NLB, k3d가 밖으로 접속한다.
구성·실행: [infra/data-migration/onprem-poc](../../infra/data-migration/onprem-poc/README.md). 비밀값·키·공인 IP는 이 문서에 적지 않는다.

## 결과 요약

| 질문 | 판정 | 증거 |
| --- | --- | --- |
| Q1 원본 EKS UDP NLB(허용 CIDR) 생성·주소 | 대기(승인 전) | |
| Q2 NAT 뒤 handshake·keepalive 25로 10분 유지 | 대기 | |
| Q3 터널 경유 원본 DB 5432 `SELECT 1` | 대기 | |
| 정리(NLB·대상 그룹·SG·ns) | 대기 | |

## 사전 조회(읽기만)

AWS Load Balancer Controller v3.6.2가 있다(in-tree 아님, Service 변형 webhook). 공개 서브넷 2개에 `kubernetes.io/role/elb=1` 태그가 있다.
원본 DB `chat-source/chat-db-0`에는 Ingress NetworkPolicy가 없어 다른 Namespace의 게이트웨이가 5432로 붙을 수 있다. 자세한 표는 [티켓 69](../../tickets/provisioning/69-onprem-data-tunnel-poc.md).

## 주의점(실측 전 예상)

- `service.beta.kubernetes.io/aws-load-balancer-type: nlb`(in-tree 방식) 대신 LBC 방식(`loadBalancerClass`)을 쓴다. LBC 기본 scheme은 internal이라 `internet-facing`을 명시해야 한다.
- UDP 대상 그룹은 IP 대상에서 클라이언트 IP가 보존된다. Pod 응답이 VPC CNI SNAT 없이 NLB로 돌아가는지가 Q2의 핵심이다.
- NLB UDP 흐름 유휴 시간(기본 120초)보다 keepalive 25초가 짧아 흐름이 유지될 것으로 본다.
