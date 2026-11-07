#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

profile=cmp-aws-prod
region=ap-northeast-2
account=714066696240
stack=cmp-demo-control-plane
actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
if [[ "$actual_account" != "$account" ]]; then
  echo "Unexpected AWS account: $actual_account" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Commit the reviewed deployment files before uploading a release." >&2
  exit 1
fi

if aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" >/dev/null 2>&1; then
  vpc="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" --query 'Stacks[0].Parameters[?ParameterKey==`VpcId`].ParameterValue|[0]' --output text)"
  subnet="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" --query 'Stacks[0].Parameters[?ParameterKey==`SubnetId`].ParameterValue|[0]' --output text)"
  ami="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" --query 'Stacks[0].Parameters[?ParameterKey==`AmiId`].ParameterValue|[0]' --output text)"
else
  vpc="$(aws ec2 describe-vpcs --profile "$profile" --region "$region" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
  subnet="$(aws ec2 describe-subnets --profile "$profile" --region "$region" --filters Name=vpc-id,Values="$vpc" Name=availability-zone,Values=ap-northeast-2a --query 'Subnets[0].SubnetId' --output text)"
  ami="$(aws ssm get-parameter --profile "$profile" --region "$region" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)"
fi
if [[ "$vpc" == None || "$subnet" == None || "$ami" == None ]]; then
  echo "Default VPC, subnet, or AL2023 AMI missing." >&2
  exit 1
fi

aws cloudformation deploy \
  --profile "$profile" --region "$region" \
  --stack-name "$stack" \
  --template-file infra/demo/cloudformation.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    AmiId="$ami" SubnetId="$subnet" VpcId="$vpc" \
    StateBucketName="cmp-demo-${account}-apne2-state" \
    DeployBucketName="cmp-demo-${account}-apne2-deploy" \
    InstanceType="${CMP_DEMO_INSTANCE_TYPE:-m7i.2xlarge}"

bash infra/demo/release.sh "${1:-HEAD}"
