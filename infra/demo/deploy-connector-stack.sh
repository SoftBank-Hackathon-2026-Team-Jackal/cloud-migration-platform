#!/usr/bin/env bash
# Create or update the connection principal stack and publish the connection templates (ticket 46,
# #181). Never touches cmp-demo-control-plane. Remote change: run only after approval.
#   deploy-connector-stack.sh --plan   # create a change set only and print it (no change applied)
#   deploy-connector-stack.sh          # create/update, upload connections/v1, print .env lines
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

profile=cmp-aws-prod
region=ap-northeast-2
account=714066696240
control_stack=cmp-demo-control-plane
stack=cmp-demo-connector
bucket="cmp-demo-${account}-apne2-connections"
template_version=v1
mode="${1:-apply}"
[[ "$mode" == apply || "$mode" == --plan ]] || { echo "Use --plan or no argument" >&2; exit 2; }

actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$actual_account" == "$account" ]] || { echo "Unexpected AWS account: $actual_account" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit the reviewed templates first" >&2; exit 1; }

# Read-only lookup on the existing stack.
role="$(aws cloudformation describe-stack-resource --profile "$profile" --region "$region" \
  --stack-name "$control_stack" --logical-resource-id InstanceRole \
  --query StackResourceDetail.PhysicalResourceId --output text)"
[[ "$role" == "${control_stack}-InstanceRole-"* ]] || { echo "Unexpected instance role: $role" >&2; exit 1; }

extra=()
[[ "$mode" == --plan ]] && extra=(--no-execute-changeset)
aws cloudformation deploy \
  --profile "$profile" --region "$region" \
  --stack-name "$stack" \
  --template-file infra/demo/connector-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  ${extra[@]+"${extra[@]}"} \
  --parameter-overrides InstanceRoleName="$role" TemplateBucketName="$bucket" \
  --tags Project=cloud-migration-platform Purpose=hackathon-demo-connections
[[ "$mode" == --plan ]] && exit 0

output() {
  aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue|[0]" --output text
}
principal="$(output PlatformPrincipalArn)"
base_url="$(output TemplateBaseUrl)"
[[ "$principal" == "arn:aws:iam::${account}:role/cmp-platform-connector" ]] \
  || { echo "Unexpected principal: $principal" >&2; exit 1; }
[[ "$base_url" == "https://${bucket}.s3.${region}.amazonaws.com/connections/${template_version}" ]] \
  || { echo "Unexpected template URL: $base_url" >&2; exit 1; }

# connections/<version>/ is immutable once users have stacks from it (ADR-005): upload a missing
# template, keep an identical one, refuse a changed one (raise template_version instead).
for template in infra/connections/source-role.yaml infra/connections/target-role.yaml; do
  key="connections/${template_version}/$(basename "$template")"
  local_sha="$(openssl dgst -sha256 -r "$template" | cut -d' ' -f1)"
  remote_sha="$(aws s3api head-object --profile "$profile" --region "$region" --bucket "$bucket" \
    --key "$key" --query 'Metadata.sha256' --output text 2>/dev/null || true)"
  if [[ -z "$remote_sha" || "$remote_sha" == None ]]; then
    aws s3api put-object --profile "$profile" --region "$region" --bucket "$bucket" --key "$key" \
      --body "$template" --content-type text/yaml --metadata "sha256=$local_sha" >/dev/null
  elif [[ "$remote_sha" != "$local_sha" ]]; then
    echo "s3://$bucket/$key differs from $template; raise template_version" >&2
    exit 1
  fi
done

cat <<EOF
Add to /opt/cmp/.env on the demo host (SSM session), then run release.sh:
CONNECTION_PLATFORM_PRINCIPAL_ARN=$principal
CONNECTION_TEMPLATE_BASE_URL=$base_url
CONNECTION_PLATFORM_AWS_PROFILE=cmp-platform-connector
EOF
