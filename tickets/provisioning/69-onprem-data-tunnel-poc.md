# Ticket 69 — 온프레미스 데이터 터널 2안 실측 PoC

## 상태
IN_PROGRESS (PoC 준비 완료, 원본 EKS·AWS 변경 사용자 승인 대기) — [Issue #289](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/289). 결선 데모(원본 EKS → 맥 k3d) 데이터 이전 1단계.

## 배경
결선 데모 대상은 NAT 뒤 맥 k3d다. 사용자 결정 "2안": 데이터 WireGuard 게이트웨이(`cmp_data.tunnel`, EKS→GKE 검증)를 재사용하되 UDP LB 입구를 **원본 EKS** 쪽에 두고, k3d 게이트웨이가 밖으로 접속한다. 가장 큰 위험(EKS UDP NLB, NAT 유지, 터널 경유 DB 접속)을 먼저 실측한다.

## 관련 요구사항
PROV-021, 데이터 DM-011(참조만)

## 선행 작업
Blocked by: 사용자 승인(원본 EKS·AWS 변경). 데이터 ticket 06·10(크로스 클라우드 실습·게이트웨이 매니페스트) 결과를 참조한다.

## 작업 범위
- 포함: 일회용 매니페스트 생성기·배치·확인·정리 스크립트(`infra/data-migration/onprem-poc/`), 생성 단위 시험, 결과 보고서
- 제외: 데이터·Agent·중계 코드 변경, 제품 Workflow 연결(결과에 따라 2·3·6단계)

## 확인할 질문·수용 기준
- [ ] Q1 원본 EKS에 UDP 51820 NLB + `loadBalancerSourceRanges` Service가 만들어지고 주소가 나온다
- [ ] Q2 k3d 게이트웨이가 NLB로 handshake 하고 keepalive 25로 10분 이상 유지된다
- [ ] Q3 k3d Pod → 터널 → 원본 DB:5432 `SELECT 1`(읽기 전용)
- [ ] NLB·대상 그룹·SG·양쪽 Namespace 정리 확인
- [x] 매니페스트 생성 단위 시험, `make check`

## 사전 조회 (읽기만, 2026-11-07)
| 항목 | 결과 |
| --- | --- |
| 계정·클러스터 | 714066696240, `cmp-demo-source` 1.34 ACTIVE, 노드 2대 AL2023(커널 6.12, amd64) |
| LB 컨트롤러 | AWS Load Balancer Controller v3.6.2(2/2), Service 변형 webhook 있음 → in-tree 아님. `loadBalancerClass: service.k8s.aws/nlb`로 명시 |
| 서브넷 태그 | 공개 2개(2a·2c) `kubernetes.io/role/elb=1`, 사설 2개 `internal-elb=1` |
| NAT | 원본 NAT GW 공인 IP 1개(데이터 실습과 같음) |
| 원본 DB | `chat-source/chat-db-0`(postgres:16, headless Service `chat-db`), DB·사용자 `chat`. DB에 Ingress NetworkPolicy 없음(정책은 `chat-app` egress만). network-policy-agent 동작 |
| 맥 k3d | `k3d-cmp-local` k3s v1.36.4, arm64, linuxkit 6.10 |

## Test Seam
- `poc.eks_manifests`/`k3d_manifests`(`tests/test_onprem_data_tunnel_poc.py`): ns 한정, Secret 없음, NLB 주석·허용 CIDR, 방향(입구 쪽 endpoint 없음·k3d keepalive 25)
- 놓치는 것: 실제 NLB·NAT 동작(보고서에 실측)

## 착수 결정
| ID | 결정 | 근거 |
| --- | --- | --- |
| P1 | 터널 대역 10.211.0.0/24 | 데이터 10.210·중계 10.200과 섞이지 않게 |
| P2 | NLB IP 대상 + TCP 8080 헬스체크 | UDP 대상 그룹은 UDP 헬스체크가 없다. 대상이 unhealthy면 결과 해석이 흐려진다 |
| P3 | 허용 CIDR은 맥 공인 IP /32 | 입구 노출 최소화 |

## 관련 문서
[보고서](../../docs/engineering/onprem-data-tunnel-poc.md), [PoC README](../../infra/data-migration/onprem-poc/README.md), `packages/data/src/cmp_data/tunnel.py`, [크로스 클라우드 실습](../../infra/data-migration/crosscloud-lab/README.md)
