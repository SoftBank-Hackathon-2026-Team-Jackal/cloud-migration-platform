#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

region=ap-northeast-2
account=714066696240
stack=cmp-demo-control-plane
if [[ "${GITHUB_ACTIONS:-}" != true ]]; then
  export AWS_PROFILE=cmp-aws-prod
fi
actual_account="$(aws sts get-caller-identity --region "$region" --query Account --output text)"
[[ "$actual_account" == "$account" ]] || { echo "Unexpected AWS account: $actual_account" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit changes before deployment" >&2; exit 1; }

requested="${1:-HEAD}"
if [[ "$requested" != HEAD && ! "$requested" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "Use HEAD or a Git commit SHA" >&2
  exit 1
fi
revision="$(git rev-parse --verify "$requested^{commit}")"
git cat-file -e "$revision:infra/demo/start.sh"
# Only main is deployed, from Actions and from a laptop alike, and the control script (activate.sh)
# always comes from main: a feature-branch checkout must not change how the host activates releases.
# CMP_ALLOW_UNMERGED=1 deploys a revision that is not on main (a deliberate, local-only exception).
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  main_ref=HEAD
else
  git fetch --quiet origin main
  main_ref=origin/main
fi
if ! git merge-base --is-ancestor "$revision" "$main_ref"; then
  if [[ "${GITHUB_ACTIONS:-}" != true && "${CMP_ALLOW_UNMERGED:-}" == 1 ]]; then
    echo "warning: deploying $revision, which is not on main (CMP_ALLOW_UNMERGED=1)" >&2
  else
    echo "Revision is not on main" >&2
    exit 1
  fi
fi
git cat-file -e "$main_ref:infra/demo/activate.sh"
short_revision="${revision:0:12}"
control_revision="$(git rev-parse --short=12 "$main_ref")"
# CMP_DEPLOY_FORCE=1 deploys even while Temporal has running workflows (activate.sh refuses otherwise).
activate_mode=""
[[ "${CMP_DEPLOY_FORCE:-}" == 1 ]] && activate_mode=force

instance="$(aws cloudformation describe-stacks --region "$region" --stack-name "$stack" --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue|[0]' --output text)"
bucket="$(aws cloudformation describe-stacks --region "$region" --stack-name "$stack" --query 'Stacks[0].Outputs[?OutputKey==`DeployBucket`].OutputValue|[0]' --output text)"
[[ "$instance" == i-* && "$bucket" == "cmp-demo-${account}-apne2-deploy" ]] || { echo "Unexpected deployment target" >&2; exit 1; }

archive="$(mktemp /tmp/cmp-demo-release.XXXXXXXX.tar.gz)"
request="$(mktemp /tmp/cmp-demo-ssm.XXXXXXXX.json)"
trap 'rm -f "$archive" "$request"' EXIT
git archive "$revision" | gzip >"$archive"
aws s3 cp "$archive" "s3://$bucket/releases/$short_revision.tar.gz" --region "$region" --only-show-errors
control_file="$(mktemp /tmp/cmp-demo-control.XXXXXXXX.sh)"
trap 'rm -f "$archive" "$request" "$control_file"' EXIT
git show "$main_ref:infra/demo/activate.sh" >"$control_file"
aws s3 cp "$control_file" "s3://$bucket/releases/control-$control_revision.sh" --region "$region" --only-show-errors

for attempt in {1..60}; do
  status="$(aws ssm describe-instance-information --region "$region" --filters "Key=InstanceIds,Values=$instance" --query 'InstanceInformationList[0].PingStatus' --output text)"
  [[ "$status" == Online ]] && break
  sleep 5
done
[[ "$status" == Online ]] || { echo "Instance is not connected to SSM" >&2; exit 1; }

python3 - "$request" "$instance" "$bucket" "$short_revision" "$control_revision" "$activate_mode" <<'PY'
import json
import sys

path, instance, bucket, revision, control_revision, mode = sys.argv[1:]
release = f"/opt/cmp/releases/{revision}"
control = f"/opt/cmp/deploy-control/{control_revision}/activate.sh"
command = (
    "set -euo pipefail; "
    f"mkdir -p {release} /opt/cmp/deploy-control/{control_revision}; "
    f"aws s3 cp s3://{bucket}/releases/{revision}.tar.gz /tmp/cmp-release.tar.gz --only-show-errors; "
    f"aws s3 cp s3://{bucket}/releases/control-{control_revision}.sh {control} --only-show-errors; "
    f"tar -xzf /tmp/cmp-release.tar.gz -C {release}; "
    f"bash {control} {revision} {mode}".rstrip()
)
with open(path, "w", encoding="utf-8") as target:
    json.dump({"InstanceIds": [instance], "DocumentName": "AWS-RunShellScript", "Parameters": {"commands": [command]}}, target)
PY
command_id="$(aws ssm send-command --region "$region" --cli-input-json "file://$request" --query Command.CommandId --output text)"
for attempt in {1..120}; do
  status="$(aws ssm get-command-invocation --region "$region" --command-id "$command_id" --instance-id "$instance" --query Status --output text 2>/dev/null || true)"
  case "$status" in
    Success|Failed|Cancelled|TimedOut) break ;;
  esac
  sleep 5
done
aws ssm get-command-invocation --region "$region" --command-id "$command_id" --instance-id "$instance" --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}' --output json
[[ "$status" == Success ]]
