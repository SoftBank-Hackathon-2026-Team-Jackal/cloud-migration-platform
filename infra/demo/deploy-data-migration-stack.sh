#!/usr/bin/env bash
# Create or update the data migration access stack (data-migration ticket 12). Never touches
# cmp-demo-control-plane or the source connection role.
#   deploy-data-migration-stack.sh --plan   # change set only, nothing applied
#   deploy-data-migration-stack.sh          # stack + data-migration-rbac.yaml, prints the .env line
# CMP_DATA_VERIFIER_ARN=<operator role ARN> trusts that role too, only while checking
# `kubectl auth can-i` as the real role; run again without it to remove the trust.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

profile=cmp-aws-prod
region=ap-northeast-2
account=714066696240
cluster=cmp-demo-source
control_stack=cmp-demo-control-plane
stack=cmp-demo-data-migration
mode="${1:-apply}"
[[ "$mode" == apply || "$mode" == --plan ]] || { echo "Use --plan or no argument" >&2; exit 2; }

actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$actual_account" == "$account" ]] || { echo "Unexpected AWS account: $actual_account" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit the reviewed template first" >&2; exit 1; }

role="$(aws cloudformation describe-stack-resource --profile "$profile" --region "$region" \
  --stack-name "$control_stack" --logical-resource-id InstanceRole \
  --query StackResourceDetail.PhysicalResourceId --output text)"
[[ "$role" == "${control_stack}-InstanceRole-"* ]] || { echo "Unexpected instance role: $role" >&2; exit 1; }

verifier="${CMP_DATA_VERIFIER_ARN:-}"
[[ "$verifier" =~ ^(arn:aws:iam::${account}:role/[A-Za-z0-9+=,.@/_-]+)?$ ]] \
  || { echo "CMP_DATA_VERIFIER_ARN must be a role in account $account" >&2; exit 2; }

extra=()
[[ "$mode" == --plan ]] && extra=(--no-execute-changeset)
aws cloudformation deploy \
  --profile "$profile" --region "$region" \
  --stack-name "$stack" \
  --template-file infra/demo/data-migration-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  ${extra[@]+"${extra[@]}"} \
  --parameter-overrides InstanceRoleName="$role" ClusterName="$cluster" \
    VerifierPrincipalArn="$verifier" \
  --tags Project=cloud-migration-platform Purpose=hackathon-demo-data-migration
[[ "$mode" == --plan ]] && exit 0

# Kubernetes objects with the operator's identity in a throwaway kubeconfig (the default
# ~/.kube/config is never read or changed).
kube_dir="$(mktemp -d)"
trap 'rm -rf "$kube_dir"' EXIT
kubeconfig="$kube_dir/config"
aws eks update-kubeconfig --profile "$profile" --region "$region" --name "$cluster" \
  --kubeconfig "$kubeconfig" >/dev/null
kubectl --kubeconfig "$kubeconfig" apply -f infra/demo/data-migration-rbac.yaml

data_role="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" \
  --query 'Stacks[0].Outputs[?OutputKey==`DataMigrationRoleArn`].OutputValue|[0]' --output text)"
[[ "$data_role" == "arn:aws:iam::${account}:role/cmp-demo-data-migration" ]] \
  || { echo "Unexpected data migration role: $data_role" >&2; exit 1; }
cat <<EOF
Add to /opt/cmp/.env on the demo host (SSM session); ensure-secrets.sh writes the profile
cmp-demo-data-migration, and the worker uses it with DATA_MIGRATION_AWS_PROFILE=cmp-demo-data-migration:
DATA_MIGRATION_ROLE_ARN=$data_role
EOF
