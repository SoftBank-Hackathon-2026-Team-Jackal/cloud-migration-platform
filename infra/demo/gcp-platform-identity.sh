#!/usr/bin/env bash
# Ticket 48 (#183) G1-G3: GCP platform principal and GKE node service account for the demo
# project. Default is a dry run: it checks the gcloud configuration's project (read-only) and
# prints the commands. Only `--apply` creates service accounts and IAM bindings. It never creates
# a key; the key step is printed as operator guidance (G4).
#
#   infra/demo/gcp-platform-identity.sh --configuration cmp-gcp-prod [--project sbh26-prod] [--apply]
#
# Applying needs project IAM admin (roles/resourcemanager.projectIamAdmin or owner) and
# roles/iam.serviceAccountAdmin. Re-running is safe: existing accounts are kept and bindings are
# additive.
set -euo pipefail

project="sbh26-prod"
configuration=""
apply=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) project="$2"; shift 2 ;;
    --configuration) configuration="$2"; shift 2 ;;
    --apply) apply=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$configuration" || "$configuration" == "default" ]]; then
  echo "an explicit named gcloud configuration is required (--configuration)" >&2
  exit 2
fi
actual="$(gcloud config get-value project --configuration "$configuration" 2>/dev/null || true)"
if [[ "$actual" != "$project" ]]; then
  echo "gcloud configuration '$configuration' selects another project; refusing" >&2
  exit 1
fi

platform="cmp-provisioning@${project}.iam.gserviceaccount.com"
nodes="cmp-gke-nodes@${project}.iam.gserviceaccount.com"
common=(--project "$project" --configuration "$configuration" --quiet)

run() {
  printf '+'; printf ' %q' "$@"; printf '\n'
  if [[ "$apply" == 1 ]]; then
    "$@" >/dev/null
  fi
}

ensure_account() {  # name display-name
  local email="$1@${project}.iam.gserviceaccount.com"
  if [[ "$apply" == 1 ]] && gcloud iam service-accounts describe "$email" "${common[@]}" >/dev/null 2>&1; then
    printf '= %s exists\n' "$email"
    return
  fi
  run gcloud iam service-accounts create "$1" --display-name "$2" "${common[@]}"
}

[[ "$apply" == 1 ]] && echo "APPLY: project $project" || echo "DRY RUN (no changes): project $project"

ensure_account cmp-provisioning "CMP provisioning (Terraform GKE Plan/Apply, workloads)"
ensure_account cmp-gke-nodes "CMP GKE nodes"

# G1: the seven resource kinds of infra/terraform/gcp/main.tf plus 11's kubectl access.
for role in roles/container.admin roles/compute.networkAdmin roles/compute.securityAdmin; do
  run gcloud projects add-iam-policy-binding "$project" --member "serviceAccount:$platform" \
    --role "$role" --condition None --configuration "$configuration" --quiet
done
# G2: GKE's recommended minimum node role (logs and metrics).
run gcloud projects add-iam-policy-binding "$project" --member "serviceAccount:$nodes" \
  --role roles/container.defaultNodeServiceAccount --condition None \
  --configuration "$configuration" --quiet
# G1: the platform may attach only the node account to node pools (resource binding, not project).
run gcloud iam service-accounts add-iam-policy-binding "$nodes" \
  --member "serviceAccount:$platform" --role roles/iam.serviceAccountUser "${common[@]}"

cat <<EOF

Not done by this script (operator, separate approval):
  1. Key (demo, G4): on the operator machine
       gcloud iam service-accounts keys create <temporary path> --iam-account $platform --configuration $configuration
     copy it over an SSM session to /opt/cmp/secrets/gcp/key.json (root, 0600), delete the
     temporary file, and set CMP_GCP_PROJECT=$project in /opt/cmp/.env. Never commit or upload it.
  2. Plan input: node_service_account_email = $nodes
EOF
