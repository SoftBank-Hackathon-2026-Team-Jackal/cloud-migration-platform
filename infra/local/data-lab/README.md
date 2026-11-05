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
