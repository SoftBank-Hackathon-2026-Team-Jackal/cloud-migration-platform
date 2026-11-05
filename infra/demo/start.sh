#!/usr/bin/env bash
set -euo pipefail

release_dir="${1:?release directory required}"
cd "$release_dir"
install -d -m 700 /opt/cmp/data/plans /opt/cmp/data/artifact-store /opt/cmp/data/caddy

for attempt in {1..60}; do
  if docker compose version >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
docker compose version

# Keys/passwords are generated once and kept; the AWS profile file is regenerated (ticket 21).
bash infra/demo/ensure-secrets.sh /opt/cmp/.env /opt/cmp/secrets

compose=(docker compose --env-file /opt/cmp/.env -f infra/demo/compose.yaml)
"${compose[@]}" up -d platform-db temporal-db temporal temporal-ui
"${compose[@]}" run --rm api alembic upgrade head
"${compose[@]}" up -d --build api worker
# Public Agent edge is opt-in: set CMP_PUBLIC_HOST in /opt/cmp/.env after 443 ingress is approved.
if grep -q '^CMP_PUBLIC_HOST=.' /opt/cmp/.env; then
  "${compose[@]}" --profile public up -d edge
fi

sleep 3
for attempt in {1..30}; do
  worker_running="$(docker inspect --format '{{.State.Running}}' cmp-demo-worker-1 2>/dev/null || true)"
  platform_db_health="$(docker inspect --format '{{.State.Health.Status}}' cmp-demo-platform-db-1 2>/dev/null || true)"
  temporal_db_health="$(docker inspect --format '{{.State.Health.Status}}' cmp-demo-temporal-db-1 2>/dev/null || true)"
  if [[ "$worker_running" == true && "$platform_db_health" == healthy && "$temporal_db_health" == healthy ]] && curl --fail --silent http://127.0.0.1:8000/health | grep -q '"status":"ok"' \
    && docker exec cmp-demo-worker-1 sh -c 'terraform version && helm version --short && kubectl version --client && aws --version && gcloud --version && docker version && docker buildx version' >/dev/null 2>&1; then
    echo "CMP demo API healthy"
    "${compose[@]}" ps
    exit 0
  fi
  sleep 2
done
echo "CMP demo API, Worker, or database health check failed" >&2
"${compose[@]}" ps
exit 1
