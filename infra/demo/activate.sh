#!/usr/bin/env bash
set -euo pipefail

revision="${1:?release revision required}"
[[ "$revision" =~ ^[0-9a-f]{12}$ ]] || exit 2
release_dir="/opt/cmp/releases/$revision"
[[ -f "$release_dir/infra/demo/start.sh" ]] || exit 2

exec 9>/opt/cmp/deploy.lock
flock -n 9 || { echo "Another demo deployment is running" >&2; exit 1; }

previous="$(cat /opt/cmp/current-revision 2>/dev/null || true)"
if [[ -z "$previous" ]]; then
  running_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' cmp-demo-api-1 2>/dev/null || true)"
  if [[ "$running_dir" =~ /opt/cmp/releases/([0-9a-f]{12})/infra/demo ]]; then
    previous="${BASH_REMATCH[1]}"
  fi
fi

if bash "$release_dir/infra/demo/start.sh" "$release_dir" >/opt/cmp/last-deploy.log 2>&1; then
  printf '%s\n' "$revision" >/opt/cmp/current-revision
  tail -n 25 /opt/cmp/last-deploy.log
  echo "Activated revision $revision"
  exit 0
fi

tail -n 35 /opt/cmp/last-deploy.log >&2
if [[ "$previous" =~ ^[0-9a-f]{12}$ && "$previous" != "$revision" && -f "/opt/cmp/releases/$previous/infra/demo/start.sh" ]]; then
  if bash "/opt/cmp/releases/$previous/infra/demo/start.sh" "/opt/cmp/releases/$previous" >/opt/cmp/last-rollback.log 2>&1; then
    printf '%s\n' "$previous" >/opt/cmp/current-revision
    echo "Restored previous revision $previous; deployment $revision failed" >&2
  else
    tail -n 35 /opt/cmp/last-rollback.log >&2
    echo "Deployment and rollback both failed" >&2
  fi
fi
exit 1
