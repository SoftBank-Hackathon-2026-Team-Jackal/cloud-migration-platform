#!/usr/bin/env bash
# Image-based activation (opt-in). start.sh runs this when /opt/cmp/.env has CMP_DEPLOY_MODE=image.
#   bash infra/demo/start-image.sh <release-dir>
# api and worker images were built and pushed by GitHub Actions (build-images.yml), tagged with the
# 12-char release revision. This host only pulls them and swaps the containers. Because the tag is the
# release revision, activate.sh's rollback (re-running the previous release's start.sh) points at the
# previous images, which are still in the local image cache. The web is still built here (it needs
# NEXT_PUBLIC_* build args and lives in another repository).
# activate.sh holds /opt/cmp/deploy.lock around this script. Any failure exits non-zero so activate.sh
# rolls back; a missing or unauthorised image fails in the pull step, before anything running is touched.
#
# /opt/cmp/.env settings (names only):
#   CMP_IMAGE_REPO_API / CMP_IMAGE_REPO_WORKER  default ghcr.io/softbank-hackathon-2026-team-jackal/cmp-demo-{api,worker}
#   CMP_WORKER_SWAP  auto (default) | force | skip
#     auto : replace the worker only when Temporal reports no running workflow (a restart cancels
#            running Activities); otherwise keep the old worker and print a warning.
#     force: always replace. skip: never replace.
set -euo pipefail

release_dir="${1:?release directory required}"
cd "$release_dir"
tag="$(basename "$release_dir")"
[[ "$tag" =~ ^[0-9a-f]{12}$ ]] || { echo "release dir name must be the 12-char revision" >&2; exit 2; }

env_file=/opt/cmp/.env
# Read single names; .env is not sourced (it can hold JSON values that are only valid for compose).
env_get() { grep -m1 "^$1=" "$env_file" | cut -d= -f2- | tr -d '\r' || true; }

default_owner=ghcr.io/softbank-hackathon-2026-team-jackal
repo_api="$(env_get CMP_IMAGE_REPO_API)"; repo_api="${repo_api:-$default_owner/cmp-demo-api}"
repo_worker="$(env_get CMP_IMAGE_REPO_WORKER)"; repo_worker="${repo_worker:-$default_owner/cmp-demo-worker}"
worker_swap="$(env_get CMP_WORKER_SWAP)"; worker_swap="${worker_swap:-auto}"
case "$worker_swap" in auto | force | skip) ;; *) echo "CMP_WORKER_SWAP must be auto, force or skip" >&2; exit 2 ;; esac

export CMP_API_IMAGE="$repo_api:$tag" CMP_WORKER_IMAGE="$repo_worker:$tag"
public=false
grep -q '^CMP_PUBLIC_HOST=.' "$env_file" && public=true

compose=(docker compose --env-file "$env_file" -f infra/demo/compose.yaml)
[[ "$public" == true ]] && compose+=(--profile public)

# 1. Pull first. A missing tag or an unauthorised private package fails here and nothing is changed.
"${compose[@]}" pull --quiet api worker

"${compose[@]}" up -d platform-db temporal-db temporal temporal-ui prometheus jaeger

# 2. Migrate with the NEW image while the old api still serves (schema changes must stay backward compatible).
"${compose[@]}" run --rm api alembic upgrade head

# 3. Decide whether the worker may be replaced now.
swap_worker=true
if [[ "$worker_swap" == skip ]]; then
  swap_worker=false
elif [[ "$worker_swap" == auto ]] && [[ "$(docker inspect --format '{{.State.Running}}' cmp-demo-worker-1 2>/dev/null || true)" == true ]]; then
  if ! body="$(curl --silent --fail --max-time 5 --get \
    --data-urlencode 'query=ExecutionStatus="Running"' \
    http://127.0.0.1:8080/api/v1/namespaces/default/workflows 2>/dev/null)"; then
    echo "warning: Temporal API not reachable; keeping the old worker" >&2
    swap_worker=false
  elif grep -q '"workflowId"' <<<"$body"; then
    echo "warning: running workflows found; keeping the old worker (set CMP_WORKER_SWAP=force to replace anyway)" >&2
    swap_worker=false
  fi
fi

services=(api)
[[ "$swap_worker" == true ]] && services+=(worker)

# 4. Swap. --wait returns non-zero when a replaced container does not become running/healthy.
"${compose[@]}" up -d --no-build --wait --wait-timeout 120 "${services[@]}"

if [[ "$public" == true ]]; then
  # The web is built from the extracted web release when there is one; --no-deps keeps api untouched.
  if [[ -f /opt/cmp/web/current/Dockerfile ]]; then
    "${compose[@]}" up -d --build --no-deps web
  fi
  "${compose[@]}" up -d --no-build --no-deps edge
fi

# 5. Final gate, same as the build flow: API health, worker toolchain, databases.
curl --fail --silent --retry 10 --retry-delay 2 --retry-connrefused http://127.0.0.1:8000/health | grep -q '"status":"ok"'
docker exec cmp-demo-worker-1 sh -c 'git --version && terraform version && helm version --short && kubectl version --client && aws --version && gcloud --version && docker version && docker buildx version' >/dev/null
for db in cmp-demo-platform-db-1 cmp-demo-temporal-db-1; do
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "$db" 2>/dev/null || true)" == healthy ]] || { echo "$db is not healthy" >&2; exit 1; }
done

# 6. Best-effort cleanup: keep this tag and the previously active one (rollback), drop older tags.
previous="$(cat /opt/cmp/current-revision 2>/dev/null || true)"
for repo in "$repo_api" "$repo_worker"; do
  docker image ls --format '{{.Tag}}' "$repo" | while read -r t; do
    [[ "$t" =~ ^[0-9a-f]{12}$ && "$t" != "$tag" && "$t" != "$previous" ]] && docker rmi "$repo:$t" >/dev/null 2>&1 || true
  done
done

[[ "$swap_worker" == true ]] || echo "WORKER NOT REPLACED: still running the previous image" >&2
echo "CMP demo API healthy (images $tag)"
"${compose[@]}" ps
