#!/usr/bin/env bash
# 크로스 클라우드 실습 배치: 원본 EKS(ns cmp-dm-src: DB·앱·게이트웨이) ↔ 대상 GKE(ns cmp-dm-dst: DB·게이트웨이).
# 필요: EKS_KUBECONFIG, GKE_KUBECONFIG, WG_KEYS(파일: "<eks|gke> <private> <public>" 두 줄),
#       EKS_NAT_CIDRS(예: 43.200.165.233/32), APP_IMAGE(전환 실행기 이미지, 원본 앱 loadgen도 이것으로 돈다),
#       EKS_STORAGE_CLASS(기본 없음이면 지정)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; lab="$here/../../local/data-lab"
eks() { kubectl --kubeconfig "$EKS_KUBECONFIG" -n cmp-dm-src "$@"; }
gke() { kubectl --kubeconfig "$GKE_KUBECONFIG" -n cmp-dm-dst "$@"; }
key() { awk -v s="$1" -v f="$2" '$1==s {print $f}' "$WG_KEYS"; }

kubectl --kubeconfig "$EKS_KUBECONFIG" create ns cmp-dm-src --dry-run=client -o yaml | kubectl --kubeconfig "$EKS_KUBECONFIG" apply -f - >/dev/null
kubectl --kubeconfig "$GKE_KUBECONFIG" create ns cmp-dm-dst --dry-run=client -o yaml | kubectl --kubeconfig "$GKE_KUBECONFIG" apply -f - >/dev/null

# DB: 원본은 wal_level=logical, 대상은 기본값(플랫폼 차트와 같음). NodePort 입구는 쓰지 않는다.
db() { python3 - "$lab/db.yaml" "$1" "$2" <<'PY'
import sys
docs = [d for d in open(sys.argv[1]).read().split("\n---\n") if "db-peer" not in d]
out = "\n---\n".join(docs)
args = "[-c, wal_level=logical, -c, max_replication_slots=10]" if sys.argv[2] == "src" else "[]"
out = out.replace("[WAL_ARGS]", args)
if sys.argv[3]:
    out = out.replace("accessModes: [ReadWriteOnce]", f"accessModes: [ReadWriteOnce]\n        storageClassName: {sys.argv[3]}")
print(out)
PY
}
db src "${EKS_STORAGE_CLASS:-}" | eks apply -f - >/dev/null
db dst "" | gke apply -f - >/dev/null

# GKE 쪽 UDP 입구 먼저(LB 주소가 있어야 EKS 게이트웨이가 접속한다)
sed "s|__EKS_NAT_CIDRS__|$EKS_NAT_CIDRS|" "$here/k8s/gke-endpoint.yaml" | gke apply -f - >/dev/null
for _ in $(seq 60); do
  lb="$(gke get svc wg-endpoint -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"; [[ -n "$lb" ]] && break; sleep 5
done
[[ -n "$lb" ]] || { echo "GKE LB 주소가 나오지 않았습니다" >&2; exit 1; }

gateway() {  # <side> <kubectl fn> <wg ip> <peer ip> <peer side> <endpoint> <local db>
  "$2" create secret generic wg-key --from-literal=private="$(key "$1" 2)" --dry-run=client -o yaml | "$2" apply -f - >/dev/null
  sed -e "s|__WG_IP__|$3|" -e "s|__PEER_IP__|$4|" -e "s|__PEER_PUBLIC_KEY__|$(key "$5" 3)|" \
      -e "s|__PEER_ENDPOINT__|$6|" -e "s|__LOCAL_DB__|$7|" "$here/k8s/gateway.yaml" | "$2" apply -f - >/dev/null
}
gateway gke gke 10.210.0.2 10.210.0.1 eks "" db.cmp-dm-dst.svc.cluster.local
gateway eks eks 10.210.0.1 10.210.0.2 gke "$lb:51820" db.cmp-dm-src.svc.cluster.local

eks rollout status sts/db --timeout=300s; gke rollout status sts/db --timeout=300s
eks rollout status deploy/wg-gateway --timeout=300s; gke rollout status deploy/wg-gateway --timeout=300s
eks exec -i db-0 -c postgres -- sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$lab/seed.sql"

# 원본 앱(loadgen)
eks create configmap loadgen --from-file="$lab/loadgen.py" --dry-run=client -o yaml | eks apply -f - >/dev/null
sed -e "s|image: cmp-data-cutover:dev|image: $APP_IMAGE|" -e "s|db.data-lab.svc|db.cmp-dm-src.svc|" "$lab/app.yaml" | eks apply -f - >/dev/null
eks rollout status deploy/chat-app --timeout=180s
echo "GKE WireGuard endpoint $lb:51820"
