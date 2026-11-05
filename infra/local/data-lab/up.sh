#!/usr/bin/env bash
# 원본·대상 k3d 클러스터에 실습 DB를 띄운다. 클러스터는 같은 docker 네트워크(cmp-dm)에 있다.
#   k3d cluster create cmp-dm-src --network cmp-dm --no-lb; k3d cluster create cmp-dm-dst --network cmp-dm --no-lb
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
KC="${KC_DIR:-$HOME/.kube/cmp-dm}"
src=(kubectl --kubeconfig "$KC/cmp-dm-src.yaml" -n data-lab)
dst=(kubectl --kubeconfig "$KC/cmp-dm-dst.yaml" -n data-lab)
kubectl --kubeconfig "$KC/cmp-dm-src.yaml" create ns data-lab --dry-run=client -o yaml | kubectl --kubeconfig "$KC/cmp-dm-src.yaml" apply -f - >/dev/null
kubectl --kubeconfig "$KC/cmp-dm-dst.yaml" create ns data-lab --dry-run=client -o yaml | kubectl --kubeconfig "$KC/cmp-dm-dst.yaml" apply -f - >/dev/null
sed 's/\[WAL_ARGS\]/[-c, wal_level=logical, -c, max_replication_slots=10]/' "$here/db.yaml" | "${src[@]}" apply -f - >/dev/null
sed 's/args: \[WAL_ARGS\]/args: []/' "$here/db.yaml" | "${dst[@]}" apply -f - >/dev/null
"${src[@]}" rollout status sts/db --timeout=180s
"${dst[@]}" rollout status sts/db --timeout=180s
"${src[@]}" exec -i db-0 -c postgres -- sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$here/seed.sql"
echo "ok"
