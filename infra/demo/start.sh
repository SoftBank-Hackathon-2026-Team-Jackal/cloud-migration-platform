#!/usr/bin/env bash
set -euo pipefail

release_dir="${1:?release directory required}"
cd "$release_dir"
install -d -m 700 /opt/cmp/data/plans /opt/cmp/data/artifact-store /opt/cmp/data/caddy
# Ticket 45: `.env` uploads (api writes, worker reads), analysis workspaces, Claude spend ledger.
install -d -m 700 /opt/cmp/data/env-files /opt/cmp/data/analysis-workspaces /opt/cmp/data/llm
# The web container runs as uid 1000 (node) and keeps the voice model and its cache here.
install -d -m 700 -o 1000 -g 1000 /opt/cmp/data/web-models /opt/cmp/data/web-cache

for attempt in {1..60}; do
  if docker compose version >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
docker compose version

# Keys/passwords are generated once and kept; the AWS profile file is regenerated (ticket 21).
# Ticket 46: the third directory holds the API-generated connection profiles shared with the worker.
bash infra/demo/ensure-secrets.sh /opt/cmp/.env /opt/cmp/secrets /opt/cmp/data/aws-connections

# Ticket 54: user-app build steps (RUN on docker0) must not reach the instance metadata service.
sh infra/demo/build-imds-guard.sh install
# Ticket 58: QEMU emulation for the other architecture (multi-arch builds). Required, and a failed
# install fails the release, only when BUILD_PLATFORM names linux/arm64.
if grep -q '^BUILD_PLATFORM=.*linux/arm64' /opt/cmp/.env; then
  sh infra/demo/build-binfmt.sh install
else
  sh infra/demo/build-binfmt.sh install || echo "warning: build-binfmt install failed" >&2
fi

# Opt-in image mode: api/worker images were built by GitHub Actions (build-images.yml); this host only
# pulls and swaps. Without CMP_DEPLOY_MODE=image in /opt/cmp/.env the build below runs as before.
if grep -q '^CMP_DEPLOY_MODE=image[[:space:]]*$' /opt/cmp/.env; then
  exec bash infra/demo/start-image.sh "$release_dir"
fi

compose=(docker compose --env-file /opt/cmp/.env -f infra/demo/compose.yaml)
"${compose[@]}" up -d platform-db temporal-db temporal temporal-ui
# Build first: the migration must run with the new release's code, not the previously built image.
"${compose[@]}" build api worker
"${compose[@]}" run --rm api alembic upgrade head
"${compose[@]}" up -d --build api worker
# Public Agent edge is opt-in: set CMP_PUBLIC_HOST in /opt/cmp/.env after 443 ingress is approved.
if grep -q '^CMP_PUBLIC_HOST=.' /opt/cmp/.env; then
  # The web front end is built from the release that infra/demo/release-web.sh extracted, when there is one.
  if [[ -f /opt/cmp/web/current/Dockerfile ]]; then
    "${compose[@]}" --profile public up -d --build web
  fi
  "${compose[@]}" --profile public up -d edge
fi

sleep 3
for attempt in {1..30}; do
  worker_running="$(docker inspect --format '{{.State.Running}}' cmp-demo-worker-1 2>/dev/null || true)"
  platform_db_health="$(docker inspect --format '{{.State.Health.Status}}' cmp-demo-platform-db-1 2>/dev/null || true)"
  temporal_db_health="$(docker inspect --format '{{.State.Health.Status}}' cmp-demo-temporal-db-1 2>/dev/null || true)"
  if [[ "$worker_running" == true && "$platform_db_health" == healthy && "$temporal_db_health" == healthy ]] && curl --fail --silent http://127.0.0.1:8000/health | grep -q '"status":"ok"' \
    && docker exec cmp-demo-worker-1 sh -c 'git --version && terraform version && helm version --short && kubectl version --client && aws --version && gcloud --version && docker version && docker buildx version' >/dev/null 2>&1; then
    echo "CMP demo API healthy"
    if grep -q '^CMP_PUBLIC_HOST=.' /opt/cmp/.env; then
      # The public entry must answer too (edge TLS -> web). A 5xx or
      # no answer fails the release, so activate.sh rolls back instead of leaving a dead page behind.
      host="$(grep -m1 '^CMP_PUBLIC_HOST=' /opt/cmp/.env | cut -d= -f2- | tr -d '\r" ')"
      public_ok=false
      for public_attempt in {1..45}; do
        code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 \
          --resolve "$host:443:127.0.0.1" "https://$host/" 2>/dev/null || true)"
        if [[ "$code" =~ ^[234][0-9][0-9]$ ]]; then public_ok=true; break; fi
        sleep 2
      done
      [[ "$public_ok" == true ]] || { echo "Public entry https://$host/ did not answer (last: ${code:-none})" >&2; "${compose[@]}" ps; exit 1; }
      echo "CMP demo public entry healthy ($code)"
    fi
    "${compose[@]}" ps
    exit 0
  fi
  sleep 2
done
echo "CMP demo API, Worker, or database health check failed" >&2
"${compose[@]}" ps
exit 1
