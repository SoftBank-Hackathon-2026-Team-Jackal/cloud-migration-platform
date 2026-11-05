#!/usr/bin/env bash
# Create or update the separate build stack (ticket 28, #152). Never touches cmp-demo-control-plane.
#   deploy-build-stack.sh --plan   # create a change set only and print it (no change applied)
#   deploy-build-stack.sh          # create/update the stack and print the api/worker .env lines
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

profile=cmp-aws-prod
region=ap-northeast-2
account=714066696240
control_stack=cmp-demo-control-plane
stack=cmp-demo-build
mode="${1:-apply}"
[[ "$mode" == apply || "$mode" == --plan ]] || { echo "Use --plan or no argument" >&2; exit 2; }

actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$actual_account" == "$account" ]] || { echo "Unexpected AWS account: $actual_account" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit the reviewed template first" >&2; exit 1; }

# Read-only lookups on the existing stack.
role="$(aws cloudformation describe-stack-resource --profile "$profile" --region "$region" \
  --stack-name "$control_stack" --logical-resource-id InstanceRole \
  --query StackResourceDetail.PhysicalResourceId --output text)"
bucket="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" \
  --stack-name "$control_stack" \
  --query 'Stacks[0].Outputs[?OutputKey==`DeployBucket`].OutputValue|[0]' --output text)"
[[ "$role" == "${control_stack}-InstanceRole-"* ]] || { echo "Unexpected instance role: $role" >&2; exit 1; }
[[ "$bucket" == "cmp-demo-${account}-apne2-deploy" ]] || { echo "Unexpected deploy bucket: $bucket" >&2; exit 1; }

extra=()
[[ "$mode" == --plan ]] && extra=(--no-execute-changeset)
aws cloudformation deploy \
  --profile "$profile" --region "$region" \
  --stack-name "$stack" \
  --template-file infra/demo/build-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  ${extra[@]+"${extra[@]}"} \
  --parameter-overrides InstanceRoleName="$role" DeployBucketName="$bucket" \
  --tags Project=cloud-migration-platform Purpose=hackathon-demo-builds
[[ "$mode" == --plan ]] && exit 0

uri="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" \
  --query 'Stacks[0].Outputs[?OutputKey==`RepositoryUri`].OutputValue|[0]' --output text)"
# Ticket 41 (#176): the API's platform registry host is the repository URI's host part.
registry="${uri%%/*}"
[[ "$registry" == "${account}.dkr.ecr.${region}.amazonaws.com" ]] \
  || { echo "Unexpected repository URI: $uri" >&2; exit 1; }
cat <<EOF
Add to /opt/cmp/.env on the demo host (SSM session), then run release.sh:
PLATFORM_ECR_REGISTRY=$registry
BUILD_ECR_REPOSITORY=$uri
BUILD_SOURCE_BUCKET=$bucket
BUILD_LOG_BUCKET=$bucket
BUILD_PLATFORM=linux/amd64
EOF
