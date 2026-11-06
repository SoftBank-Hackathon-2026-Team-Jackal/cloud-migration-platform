#!/usr/bin/env bash
# Read-only demo server check (ticket 45, #180). Run as root in an SSM session:
#   bash /opt/cmp/releases/<SHA>/infra/demo/runtime-check.sh [/opt/cmp]
# Prints `name=state` lines and `.env` NAMES only, never a value. Changes nothing.
set -uo pipefail

root="${1:-/opt/cmp}"
env_file="$root/.env"

echo "revision=$(cat "$root/current-revision" 2>/dev/null || echo unknown)"
docker ps --format '{{.Names}} {{.Status}}' --filter name=cmp-demo- 2>/dev/null \
  | sed 's/^/container=/' || echo "container=unavailable"
echo "alembic=$(docker exec cmp-demo-api-1 alembic current 2>/dev/null | tail -n 1 || true)"

# Which real paths the worker registers and what the API has configured (state words only).
docker exec cmp-demo-worker-1 python -m cmp_worker.runtime_report 2>/dev/null \
  || echo "worker_report=unavailable"
docker exec cmp-demo-api-1 python -m cmp_api.runtime_report 2>/dev/null \
  || echo "api_report=unavailable"

# Ticket 54: build steps must not reach the instance metadata service.
echo "imds_guard_rules=$(iptables -S DOCKER-USER 2>/dev/null | grep -c cmp-build-imds-guard || true)"
echo "imds_guard_service=$(systemctl is-enabled cmp-build-imds-guard.service 2>/dev/null || echo missing)"

# `.env` names (values are never read into the output).
names="$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$env_file" 2>/dev/null | sort -u | tr '\n' ' ')"
echo "env_names=${names% }"
missing=()
# Required by compose (`:?`); start.sh's ensure-secrets.sh generates them when absent.
for name in PLATFORM_DB_PASSWORD TEMPORAL_DB_PASSWORD AGENT_ENROLLMENT_ADMIN_KEY \
  APPROVAL_ADMIN_KEY ENV_FILE_ENCRYPTION_KEY; do
  [[ " $names " == *" $name "* ]] || missing+=("$name")
done
echo "missing=${missing[*]:-}"
optional=()
# Optional: empty keeps that feature off (README "서버 점검").
# Tickets 47·48: PLATFORM_ECR_PULL_ROLE_ARN·PLATFORM_ECR_AWS_PROFILE (pull-only role),
# PROVISIONING_PLATFORM_EGRESS_CIDRS (EIP /32), CMP_GCP_PROJECT (with secrets/gcp/key.json).
for name in CMP_PUBLIC_HOST CMP_GCP_PROJECT PLATFORM_ECR_REGISTRY PLATFORM_ECR_PULL_ROLE_ARN \
  PLATFORM_ECR_AWS_PROFILE PROVISIONING_PLATFORM_EGRESS_CIDRS BUILD_ECR_REPOSITORY \
  BUILD_SOURCE_BUCKET BUILD_LOG_BUCKET BUILD_PLATFORM CONNECTION_PLATFORM_PRINCIPAL_ARN \
  CONNECTION_TEMPLATE_BASE_URL ANALYSIS_ARTIFACT_STORE_DIR ANALYSIS_WORKSPACE_ROOT \
  SANDBOX_KUBECONFIG LLM_ENABLED ANTHROPIC_API_KEY WEB_START_SANDBOX_SETTINGS \
  WEB_START_DATA_MIGRATION; do
  [[ " $names " == *" $name "* ]] || optional+=("$name")
done
echo "optional_unset=${optional[*]:-}"
echo "target_profiles_file=$([[ -f "$root/config/target-profiles.json" ]] && echo present || echo absent)"
echo "sandbox_kubeconfig_file=$([[ -f "$root/secrets/sandbox/kubeconfig" ]] && echo present || echo absent)"
echo "gcp_key_file=$([[ -f "$root/secrets/gcp/key.json" ]] && echo present || echo absent)"
