#!/usr/bin/env bash
# Activate a release on the demo host (run by release.sh over SSM as root).
#   bash activate.sh <12-char revision> [force]
# - Waits up to 15 min for /opt/cmp/deploy.lock (release-web.sh takes the same lock) instead of failing.
# - Refuses to start while Temporal has running workflows: restarting the worker cancels their
#   Activities, so a deployment would break a migration in progress. `force` deploys anyway.
# - A failed deployment keeps its log as failed-deploy-<revision>-<time>.log (newest 5) before the
#   rollback overwrites anything, and every attempt appends one line to deploy-history.log:
#   time, revision, web revision, sha256 prefixes of .env and web.env (fingerprints, never values), result.
set -euo pipefail

revision="${1:?release revision required}"
mode="${2:-}"
[[ "$revision" =~ ^[0-9a-f]{12}$ ]] || exit 2
[[ -z "$mode" || "$mode" == force ]] || exit 2
release_dir="/opt/cmp/releases/$revision"
[[ -f "$release_dir/infra/demo/start.sh" ]] || exit 2

exec 9>/opt/cmp/deploy.lock
flock -w 900 9 || { echo "Another demo deployment is still running after 15 min" >&2; exit 1; }

fingerprint() { if [[ -f "$1" ]]; then sha256sum "$1" | cut -c1-12; else echo absent; fi; }
record() {
  printf '%s rev=%s web=%s env=%s web_env=%s result=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$revision" \
    "$(basename "$(readlink /opt/cmp/web/current 2>/dev/null || echo none)")" \
    "$(fingerprint /opt/cmp/.env)" "$(fingerprint /opt/cmp/web.env)" "$1" >>/opt/cmp/deploy-history.log
}

if [[ "$mode" != force ]]; then
  # Temporal UI's HTTP API on loopback (compose temporal-ui). Unknown is treated as "maybe running".
  if ! running="$(curl --silent --fail --max-time 5 --get \
    --data-urlencode 'query=ExecutionStatus="Running"' \
    http://127.0.0.1:8080/api/v1/namespaces/default/workflows 2>/dev/null)"; then
    if [[ "$(docker inspect --format '{{.State.Running}}' cmp-demo-temporal-ui-1 2>/dev/null || true)" == true ]]; then
      record refused-temporal-unreachable
      echo "Temporal API not reachable; cannot tell whether a migration is running (deploy with force to override)" >&2
      exit 1
    fi
  elif grep -q '"workflowId"' <<<"$running"; then
    record refused-running-workflows
    echo "Running workflows found; deploying would restart the worker and cancel them:" >&2
    grep -o '"workflowId":"[^"]*"' <<<"$running" | cut -d'"' -f4 | head -n 10 >&2
    echo "Wait for them to finish, or deploy with force (CMP_DEPLOY_FORCE=1 / workflow input force)." >&2
    exit 1
  fi
fi

previous="$(cat /opt/cmp/current-revision 2>/dev/null || true)"
if [[ -z "$previous" ]]; then
  running_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' cmp-demo-api-1 2>/dev/null || true)"
  if [[ "$running_dir" =~ /opt/cmp/releases/([0-9a-f]{12})/infra/demo ]]; then
    previous="${BASH_REMATCH[1]}"
  fi
fi

if bash "$release_dir/infra/demo/start.sh" "$release_dir" >/opt/cmp/last-deploy.log 2>&1; then
  printf '%s\n' "$revision" >/opt/cmp/current-revision
  record success
  # Each server-side build leaves the previous image untagged (~1.4-1.8 GB each). Dangling images only.
  docker image prune -f >/dev/null 2>&1 || true
  tail -n 25 /opt/cmp/last-deploy.log
  echo "Activated revision $revision"
  exit 0
fi

failed_log="/opt/cmp/failed-deploy-$revision-$(date -u +%Y%m%dT%H%M%SZ).log"
cp /opt/cmp/last-deploy.log "$failed_log"
ls -1t /opt/cmp/failed-deploy-*.log 2>/dev/null | tail -n +6 | xargs -r rm -f
record failed
tail -n 35 /opt/cmp/last-deploy.log >&2
echo "Failure log kept at $failed_log" >&2
if [[ "$previous" =~ ^[0-9a-f]{12}$ && "$previous" != "$revision" && -f "/opt/cmp/releases/$previous/infra/demo/start.sh" ]]; then
  if bash "/opt/cmp/releases/$previous/infra/demo/start.sh" "/opt/cmp/releases/$previous" >/opt/cmp/last-rollback.log 2>&1; then
    printf '%s\n' "$previous" >/opt/cmp/current-revision
    record "rolled-back-to-$previous"
    echo "Restored previous revision $previous; deployment $revision failed" >&2
  else
    record rollback-failed
    tail -n 35 /opt/cmp/last-rollback.log >&2
    echo "Deployment and rollback both failed" >&2
  fi
fi
exit 1
