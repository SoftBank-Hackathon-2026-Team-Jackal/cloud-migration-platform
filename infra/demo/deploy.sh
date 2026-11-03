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

vpc="$(aws ec2 describe-vpcs --profile "$profile" --region "$region" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
subnet="$(aws ec2 describe-subnets --profile "$profile" --region "$region" --filters Name=vpc-id,Values="$vpc" Name=availability-zone,Values=ap-northeast-2a --query 'Subnets[0].SubnetId' --output text)"
ami="$(aws ssm get-parameter --profile "$profile" --region "$region" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)"
if [[ "$vpc" == None || "$subnet" == None || "$ami" == None ]]; then
  echo "Default VPC, subnet, or AL2023 AMI missing." >&2
  exit 1
fi

aws cloudformation deploy \
  --profile "$profile" --region "$region" \
  --stack-name "$stack" \
  --template-file infra/demo/cloudformation.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    AmiId="$ami" SubnetId="$subnet" VpcId="$vpc" \
    StateBucketName="cmp-demo-${account}-apne2-state" \
    DeployBucketName="cmp-demo-${account}-apne2-deploy" \
    InstanceType=t3.large

instance="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue|[0]' --output text)"
bucket="$(aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$stack" --query 'Stacks[0].Outputs[?OutputKey==`DeployBucket`].OutputValue|[0]' --output text)"
revision="$(git rev-parse --short=12 HEAD)"
archive="$(mktemp /tmp/cmp-demo-release.XXXXXXXX.tar.gz)"
request="$(mktemp /tmp/cmp-demo-ssm.XXXXXXXX.json)"
trap 'rm -f "$archive" "$request"' EXIT
git archive HEAD | gzip > "$archive"
aws s3 cp "$archive" "s3://$bucket/releases/$revision.tar.gz" --profile "$profile" --region "$region" --only-show-errors

echo "Waiting for SSM registration: $instance"
for attempt in {1..60}; do
  status="$(aws ssm describe-instance-information --profile "$profile" --region "$region" --filters "Key=InstanceIds,Values=$instance" --query 'InstanceInformationList[0].PingStatus' --output text)"
  [[ "$status" == Online ]] && break
  sleep 5
done
if [[ "$status" != Online ]]; then
  echo "Instance did not register with SSM." >&2
  exit 1
fi

python3 - "$request" "$instance" "$bucket" "$revision" <<'PY'
import json
import sys

path, instance, bucket, revision = sys.argv[1:]
release = f"/opt/cmp/releases/{revision}"
command = (
    "set -euo pipefail; "
    f"mkdir -p {release}; "
    f"aws s3 cp s3://{bucket}/releases/{revision}.tar.gz /tmp/cmp-release.tar.gz; "
    f"tar -xzf /tmp/cmp-release.tar.gz -C {release}; "
    f"if bash {release}/infra/demo/start.sh {release} > /tmp/cmp-demo-start.log 2>&1; "
    "then tail -n 30 /tmp/cmp-demo-start.log; "
    "else tail -n 30 /tmp/cmp-demo-start.log; exit 1; fi"
)
with open(path, "w", encoding="utf-8") as target:
    json.dump({"InstanceIds": [instance], "DocumentName": "AWS-RunShellScript", "Parameters": {"commands": [command]}}, target)
PY
command_id="$(aws ssm send-command --profile "$profile" --region "$region" --cli-input-json "file://$request" --query Command.CommandId --output text)"
for attempt in {1..120}; do
  status="$(aws ssm get-command-invocation --profile "$profile" --region "$region" --command-id "$command_id" --instance-id "$instance" --query Status --output text 2>/dev/null || true)"
  case "$status" in
    Success) break ;;
    Failed|Cancelled|TimedOut) break ;;
  esac
  sleep 5
done
aws ssm get-command-invocation --profile "$profile" --region "$region" --command-id "$command_id" --instance-id "$instance" --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}' --output json
[[ "$status" == Success ]]
