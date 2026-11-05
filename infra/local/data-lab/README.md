# 데이터 이전 로컬 실습 (k3d 두 개)

원본·대상 클러스터를 따로 두고, 각 클러스터의 PostgreSQL 16 Pod 안에서 psql로 CDC를 실행한다(DM-001).
원본 DB는 데모 chat-source(EKS)와, 대상 DB는 플랫폼 postgres 차트와 같은 모양이다(대상은 wal_level 기본값).
두 DB는 NodePort(30432)로 서로 닿는다. 실제 환경에서는 이 자리가 WireGuard 터널 주소다(DM-011, ticket 06).

```bash
docker network create cmp-dm
k3d cluster create cmp-dm-src --network cmp-dm --no-lb --kubeconfig-update-default=false
k3d cluster create cmp-dm-dst --network cmp-dm --no-lb --kubeconfig-update-default=false
mkdir -p ~/.kube/cmp-dm
k3d kubeconfig get cmp-dm-src > ~/.kube/cmp-dm/cmp-dm-src.yaml
k3d kubeconfig get cmp-dm-dst > ~/.kube/cmp-dm/cmp-dm-dst.yaml
infra/local/data-lab/up.sh

# 노드 주소(상대 DB가 접속할 주소)
kubectl --kubeconfig ~/.kube/cmp-dm/cmp-dm-src.yaml get nodes -o wide

DATA_MIGRATION_KUBECONFIG_DIR=~/.kube/cmp-dm DATA_LAB_SRC_HOST=<src 노드> DATA_LAB_DST_HOST=<dst 노드> \
  uv run pytest tests/data/test_data_lab.py
```

정리: `k3d cluster delete cmp-dm-src cmp-dm-dst && docker network rm cmp-dm`

## 전환·롤백 실습 (ticket 04·06)

```bash
docker build -t cmp-data-cutover:dev infra/data-migration/cutover
k3d image import cmp-data-cutover:dev -c cmp-dm-src
WITH_APP=1 infra/local/data-lab/up.sh     # 원본 앱(loadgen, 초당 약 50건 쓰기)
kubectl --kubeconfig ~/.kube/cmp-dm/cmp-dm-src.yaml -n data-lab logs -f deploy/chat-app   # {"ok","err","last"}
```

원본 `DbEndpoint`에는 `local_host: db.data-lab.svc`를 넣는다(cmp-cutover Pod가 원본 DB로 가는 주소).
Activity 순서: `dm_precheck` → `dm_prepare` → `dm_copy_schema` → `dm_subscribe` → `dm_sample`(streaming까지)
→ `dm_install_cutover` → `dm_route_app` → `dm_switchover` → (롤백) `dm_switchover`(`direction: reverse`).
잃은 쓰기 확인: 앱 로그의 `last`까지 `lg-<pod>-<순번>`이 새 주 DB에 모두 있는지 센다.
