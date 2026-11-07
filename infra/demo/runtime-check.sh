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

# Ticket 58: whether the worker's builder can build each platform, and whether BUILD_PLATFORM fits
# (state words only, never the value). The first BUILD_PLATFORM entry is the Sandbox's
# architecture, which is this host's.
echo "build_binfmt_service=$(systemctl is-enabled cmp-build-binfmt.service 2>/dev/null || echo missing)"
builder="$(docker exec cmp-demo-worker-1 docker buildx inspect default 2>/dev/null \
  | sed -n 's/^Platforms: *//p' | head -n 1 | tr -d ' ')"
for arch in amd64 arm64; do
  if [[ -z "$builder" ]]; then state=unavailable
  elif [[ ",$builder," == *",linux/$arch,"* ]]; then state=yes
  else state=no; fi
  echo "builder_$arch=$state"
done
wanted="$(sed -n 's/^BUILD_PLATFORM=//p' "$env_file" 2>/dev/null | tail -n 1 | tr -d ' "')"
if [[ -z "$wanted" ]]; then
  echo "build_platforms_supported=unset"
else
  supported=yes
  IFS=, read -ra platforms <<<"$wanted"
  for platform in "${platforms[@]}"; do
    [[ ",$builder," == *",$platform,"* ]] || supported=no
  done
  echo "build_platforms_supported=$supported"
  case "$(uname -m)" in x86_64) host=linux/amd64 ;; aarch64) host=linux/arm64 ;; *) host=unknown ;; esac
  echo "build_platform_first_is_host=$([[ "${platforms[0]}" == "$host" ]] && echo yes || echo no)"
fi

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
  SANDBOX_KUBECONFIG LLM_ENABLED ANTHROPIC_API_KEY AGENT_CHAT_ENABLED PULSE_REPORT_TOKEN WEB_START_SANDBOX_SETTINGS \
  WEB_START_DATA_MIGRATION WEB_START_TRAFFIC DATA_MIGRATION_AWS_PROFILE \
  DATA_MIGRATION_KUBECONFIG_DIR; do
  [[ " $names " == *" $name "* ]] || optional+=("$name")
done
echo "optional_unset=${optional[*]:-}"
echo "target_profiles_file=$([[ -f "$root/config/target-profiles.json" ]] && echo present || echo absent)"
echo "sandbox_kubeconfig_file=$([[ -f "$root/secrets/sandbox/kubeconfig" ]] && echo present || echo absent)"
# Provisioning 72: how many kubeconfig files the data-migration mount holds (count only, no names).
echo "data_migration_kubeconfig_files=$(find "$root/secrets/data-migration" -maxdepth 1 -name '*.yaml' 2>/dev/null | wc -l | tr -d ' ')"
echo "gcp_key_file=$([[ -f "$root/secrets/gcp/key.json" ]] && echo present || echo absent)"
