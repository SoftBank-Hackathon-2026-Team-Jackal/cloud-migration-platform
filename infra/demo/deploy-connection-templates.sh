#!/usr/bin/env bash
# Create or update the connection template bucket stack and publish infra/connections/*.yaml
# (ticket 23b, #205; ADR-005). Never touches cmp-demo-control-plane.
#   deploy-connection-templates.sh --plan   # create a change set only (no change applied)
#   deploy-connection-templates.sh          # apply, upload both templates, print the .env lines
# The AssumeRole permission is in the build stack: run deploy-build-stack.sh as well.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

profile=cmp-aws-prod
region=ap-northeast-2
account=714066696240
control_stack=cmp-demo-control-plane
stack=cmp-demo-connection-templates
# Bump together with any change to infra/connections/*.yaml (published objects never change).
version=v1
templates=(source-role.yaml target-role.yaml)
mode="${1:-apply}"
[[ "$mode" == apply || "$mode" == --plan ]] || { echo "Use --plan or no argument" >&2; exit 2; }

actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$actual_account" == "$account" ]] || { echo "Unexpected AWS account: $actual_account" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit the reviewed templates first" >&2; exit 1; }

# Read-only lookups: the platform principal is the existing demo instance role.
role="$(aws cloudformation describe-stack-resource --profile "$profile" --region "$region" \
  --stack-name "$control_stack" --logical-resource-id InstanceRole \
  --query StackResourceDetail.PhysicalResourceId --output text)"
[[ "$role" == "${control_stack}-InstanceRole-"* ]] || { echo "Unexpected instance role: $role" >&2; exit 1; }
principal="$(aws iam get-role --profile "$profile" --role-name "$role" --query Role.Arn --output text)"
[[ "$principal" == "arn:aws:iam::${account}:role/${role}" ]] \
  || { echo "Unexpected principal ARN: $principal" >&2; exit 1; }

extra=()
[[ "$mode" == --plan ]] && extra=(--no-execute-changeset)
aws cloudformation deploy \
  --profile "$profile" --region "$region" \
  --stack-name "$stack" \
  --template-file infra/demo/connection-templates-stack.yaml \
  --no-fail-on-empty-changeset \
  ${extra[@]+"${extra[@]}"} \
  --parameter-overrides TemplateVersion="$version" \
  --tags Project=cloud-migration-platform Purpose=hackathon-demo-connection-templates
[[ "$mode" == --plan ]] && exit 0

output() {
  aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue|[0]" --output text
}
bucket="$(output BucketName)"
base_url="$(output TemplateBaseUrl)"
[[ "$bucket" == "cmp-demo-${account}-apne2-connection-templates" ]] \
  || { echo "Unexpected bucket: $bucket" >&2; exit 1; }
[[ "$base_url" == "https://${bucket}.s3.${region}.amazonaws.com/connections/${version}" ]] \
  || { echo "Unexpected template URL: $base_url" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
for name in "${templates[@]}"; do
  local_file="infra/connections/$name"
  # An object already published under this version must not change under existing links.
  if curl -sf -o "$tmp/$name" "$base_url/$name"; then
    cmp -s "$tmp/$name" "$local_file" \
      || { echo "$name differs from connections/$version; bump TemplateVersion" >&2; exit 1; }
    continue
  fi
  aws s3 cp "$local_file" "s3://$bucket/connections/$version/$name" \
    --profile "$profile" --region "$region" \
    --content-type text/yaml --cache-control no-cache
done
for name in "${templates[@]}"; do
  status="$(curl -sI "$base_url/$name" | head -n 1)"
  [[ "$status" == HTTP/*" 200"* ]] || { echo "$name is not publicly readable: $status" >&2; exit 1; }
done

cat <<EOF
Add to /opt/cmp/.env on the demo host (SSM session), then run release.sh:
CONNECTION_PLATFORM_PRINCIPAL_ARN=$principal
CONNECTION_TEMPLATE_BASE_URL=$base_url
EOF
