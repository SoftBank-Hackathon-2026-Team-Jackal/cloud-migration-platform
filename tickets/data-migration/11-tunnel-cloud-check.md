# Ticket 11 — WireGuard 복제 경로 클라우드 검증과 발견한 문제 두 가지

## 상태

DONE(실제 EKS ↔ GKE 검증, 2026-11-06). 클라우드 자원은 검증 뒤 모두 지웠다.

## 검증 환경

- 원본: 데모 EKS `cmp-demo-source`(ap-northeast-2)의 실습 Namespace `cmp-dm-src`(데모 앱 `chat-source`는 건드리지 않음). 원본 DB(`wal_level=logical`)와 쓰기 앱(loadgen, 초당 약 50~60건)
- 대상: `infra/data-migration/crosscloud-lab` Terraform으로 만든 GKE(asia-northeast3-a). Provisioning GKE(`infra/terraform/gcp`)와 같게 Dataplane V2를 켰다(이 커밋에서 실습 Terraform에 추가). 대상 DB는 플랫폼 postgres 차트(`infra/helm/postgres`, Provisioning 기본 파라미터)로, 정책은 `cmp_providers.network_policy.production_policies`(GCP)로 만들었다.
- 플랫폼: 로컬 API·Temporal·worker(분석~구축은 대역). 웹과 같은 `POST /migrations`, `WEB_START_DATA_MIGRATION`에는 `tunnel: {source_egress_cidrs: [EKS NAT IP/32]}`만 넣었다.

## 발견한 문제

| # | 증상 | 원인 | 수정 |
| --- | --- | --- | --- |
| 1 | 터널 handshake는 됐지만 `dm_subscribe`가 실패(대상 DB → 게이트웨이 Service 시간 초과) | 게이트웨이 Service가 Pod 5433으로 넘겼다. 대상 DB의 Provisioning 송신 정책은 TCP 5432만 허용하고, NetworkPolicy는 DNAT 뒤 Pod 포트로 판단한다 | 게이트웨이의 "터널 너머 DB" 리스너를 Pod IP:5432(downward API `POD_IP`)로 옮기고 Service `targetPort`를 5432로. 터널 쪽 리스너는 터널 IP:5432 그대로 |
| 2 | 1을 고친 뒤에도 실패. 대상 Namespace 안 모든 Pod에서 DNS 질의 시간 초과 | GKE 1.36 새 클러스터는 NodeLocal DNSCache가 기본으로 켜진다. kube-dns Service로 가는 질의를 `k8s-app=node-local-dns` Pod가 받는데, Provisioning `cmp-allow-dns`는 `k8s-app=kube-dns`만 허용한다 | `cmp_providers.network_policy.allow_dns`에 `node-local-dns`를 더했다. **Provisioning 영역**: 이 문제는 데이터 이전만이 아니라 GCP 대상의 앱 → DB(Service 이름) 연결도 막는다 |

## 결과(문제 1·2 수정 뒤)

| 항목 | 값 |
| --- | --- |
| 터널 | GKE UDP LB(원본 NAT /32만), handshake 확인 |
| 승인 요청까지 | 시작 뒤 약 117초(터널 구성, 스키마, 초기 복사 약 18만 행, streaming) |
| 쓰기 중단 | 83.26ms(PAUSE 구간: marker 30.0ms, seq 39.4ms 등) |
| 정합성 | 테이블 3개 행 수·체크섬 일치(messages 187,868행) |
| 앱 쓰기 | 오류 0건, 최대 지연 80.4ms(전환 순간). 앱이 성공으로 받은 쓰기는 모두 GKE에 있다 |
| SSE 이벤트 | 56건 계약 통과 |
| 준비 전 승인 | 409(`phase: tunnel`) |

## 정리

GKE 실습 클러스터(Terraform destroy), 두 클러스터의 실습 Namespace(`cmp-dm-src`, `cmp-app`, `cmp-data-tunnel`), GCP LB(포워딩 규칙 0개 확인), ECR 시험 태그를 지웠다. 데모 앱 `chat-source`는 그대로다.

## 남은 일

- Provisioning 담당 확인: DNS 정책 수정(문제 2)
- 데모 서버: GCP 대상 프로필·연결, `WEB_START_DATA_MIGRATION`의 `tunnel` 값(원본 NAT `43.200.165.233/32`)
- `finish`·취소 때 게이트웨이·LB 정리 단계
