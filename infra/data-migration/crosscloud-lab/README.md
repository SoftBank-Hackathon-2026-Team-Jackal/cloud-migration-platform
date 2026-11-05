# 데이터 이전 크로스 클라우드 실습 (EKS ↔ GKE, WireGuard)

원본 EKS 클러스터의 PostgreSQL을 대상 GKE 클러스터로 옮기고 전환·롤백한다(specs/data-migration DM-010·DM-011).
원본 EKS는 기존 클러스터를 쓰고, GKE(서울, 노드 1대)만 Terraform으로 만든다.

```
AWS EKS (ns cmp-dm-src)                                  GCP GKE (ns cmp-dm-dst)
 chat-app(loadgen) → cmp-pgbouncer → db-0                  db-0
 wg-gateway 10.210.0.1 ──── WireGuard UDP 51820 ────▶ LB → wg-gateway 10.210.0.2
 Service wg-peer-db:5432 → 터널 → GKE db                    Service wg-peer-db:5432 → 터널 → EKS db
```

- WireGuard 입구는 GKE 쪽 LoadBalancer(UDP)이고, EKS NAT 공인 IP에서 오는 것만 받는다. EKS 쪽은 접속만 하므로 입구가 없다.
- 각 클러스터의 `wg-peer-db` Service가 터널 너머 DB로 이어진다. `DbEndpoint.peer_host`에 이 주소를 넣는다:
  원본 `peer_host = wg-peer-db.cmp-dm-dst.svc.cluster.local`(GKE가 원본으로 올 때), 대상 `peer_host = wg-peer-db.cmp-dm-src.svc.cluster.local`.
- 게이트웨이는 `alpine` + `wireguard-tools`·`socat`(시작 시 설치), privileged. 커널 모듈이 없으면 `wireguard-go`를 쓴다(EKS AL2023·GKE COS 모두 커널 모듈로 동작).
- WireGuard 키는 Terraform state에 남기지 않는다. 로컬에서 만들어(`wg genkey`) 게이트웨이 Secret에만 넣는다.

## 실행

```bash
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token --account=<계정>)
cp backend.hcl.example backend.hcl   # state 버킷·프로필
terraform init -backend-config=backend.hcl
terraform apply -var 'eks_nat_ips=["<EKS NAT IP>/32"]'

# 키: "<eks|gke> <private> <public>" 두 줄
docker run --rm alpine:3.22 sh -c 'apk add -q wireguard-tools; for s in eks gke; do k=$(wg genkey); echo "$s $k $(echo $k | wg pubkey)"; done' > wgkeys.txt

docker buildx build --platform linux/amd64 -t <ECR>/<repo>:<tag> --push ../cutover
EKS_KUBECONFIG=... GKE_KUBECONFIG=... WG_KEYS=wgkeys.txt EKS_NAT_CIDRS=<IP>/32 \
  APP_IMAGE=<ECR>/<repo>:<tag> EKS_STORAGE_CLASS=<sc> ./up.sh
```

Worker 설정: `DATA_MIGRATION_AWS_PROFILE`, `DATA_MIGRATION_GCLOUD_CONFIGURATION`(계정·프로젝트가 든 gcloud 구성), PATH에 `gke-gcloud-auth-plugin`.

정리: `kubectl delete ns cmp-dm-src`(EKS), `terraform destroy`(GKE·LB), ECR 시험 태그 삭제.

## 결과 (2026-11-05)

초당 약 50건 쓰기 중, 로컬 Temporal 서버 + 실제 Activity(`AWS_EKS`, `GCP_GKE` 접근):

| 항목 | 값 |
| --- | --- |
| 터널 RTT | 약 4ms |
| streaming 도달(초기 복사 2만 행) | 16.2초 |
| 전환 쓰기 중단 | 110ms, 체크섬 일치 |
| 롤백 쓰기 중단 | 56ms, 체크섬 일치 |
| 커밋된 쓰기 누락·중복 | 0 · 0 |
| 앱 오류 / 최대 쓰기 지연 | 0 / 153ms |
