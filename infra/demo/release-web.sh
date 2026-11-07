#!/usr/bin/env bash
# Upload the web front end (cloud-migration-web) to the demo host and (re)build it with docker compose.
#   bash infra/demo/release-web.sh <path-to-web-checkout> [HEAD|<sha>]
#
# - The git archive goes to s3://<deploy-bucket>/releases/web-<sha12>.tar.gz: the instance role may only
#   read releases/*, so the web archive lives next to the platform releases.
# - On the host it is extracted to /opt/cmp/web/<sha12>, /opt/cmp/web/current points at it, and the web
#   service of the active platform release is rebuilt (docker compose, profile public, --no-deps so api
#   and worker are never recreated, also in image mode). Only the three newest web releases are kept.
# - Takes /opt/cmp/deploy.lock (waits up to 15 min), the lock activate.sh holds, so a web rebuild and a
#   platform deployment never run compose at the same time.
# - Needs AWS access to cmp-aws-prod (default AWS_PROFILE=cmp-aws-prod) with write access to the deploy
#   bucket and ssm:SendCommand. Nothing secret is uploaded: GitHub OAuth values live on the host in
#   /opt/cmp/web.env (0600), generated keys in /opt/cmp/.env.
set -euo pipefail

web_repo="${1:?path to the cloud-migration-web checkout required}"
requested="${2:-HEAD}"
if [[ "$requested" != HEAD && ! "$requested" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "Use HEAD or a Git commit SHA" >&2
  exit 1
fi

region=ap-northeast-2
account=714066696240
stack=cmp-demo-control-plane
export AWS_PROFILE="${AWS_PROFILE:-cmp-aws-prod}"
actual_account="$(aws sts get-caller-identity --region "$region" --query Account --output text)"
[[ "$actual_account" == "$account" ]] || { echo "Unexpected AWS account: $actual_account" >&2; exit 1; }

git -C "$web_repo" rev-parse --git-dir >/dev/null 2>&1 || { echo "Not a git checkout: $web_repo" >&2; exit 1; }
[[ -z "$(git -C "$web_repo" status --porcelain)" ]] || { echo "Commit changes in the web checkout before deployment" >&2; exit 1; }
git -C "$web_repo" cat-file -e "$requested^{commit}:Dockerfile" 2>/dev/null || { echo "The web revision has no Dockerfile" >&2; exit 1; }
revision="$(git -C "$web_repo" rev-parse --verify "$requested^{commit}")"
short="${revision:0:12}"

instance="$(aws cloudformation describe-stacks --region "$region" --stack-name "$stack" --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue|[0]' --output text)"
bucket="$(aws cloudformation describe-stacks --region "$region" --stack-name "$stack" --query 'Stacks[0].Outputs[?OutputKey==`DeployBucket`].OutputValue|[0]' --output text)"
[[ "$instance" == i-* && "$bucket" == "cmp-demo-${account}-apne2-deploy" ]] || { echo "Unexpected deployment target" >&2; exit 1; }

archive="$(mktemp /tmp/cmp-web-release.XXXXXXXX.tar.gz)"
request="$(mktemp /tmp/cmp-web-ssm.XXXXXXXX.json)"
trap 'rm -f "$archive" "$request"' EXIT
git -C "$web_repo" archive "$revision" | gzip >"$archive"
aws s3 cp "$archive" "s3://$bucket/releases/web-$short.tar.gz" --region "$region" --only-show-errors

for attempt in {1..60}; do
  status="$(aws ssm describe-instance-information --region "$region" --filters "Key=InstanceIds,Values=$instance" --query 'InstanceInformationList[0].PingStatus' --output text)"
  [[ "$status" == Online ]] && break
  sleep 5
done
[[ "$status" == Online ]] || { echo "Instance is not connected to SSM" >&2; exit 1; }

python3 - "$request" "$instance" "$bucket" "$short" <<'PY'
import json
import sys

path, instance, bucket, revision = sys.argv[1:]
web = f"/opt/cmp/web/{revision}"
command = (
    "set -euo pipefail; "
    # Same lock as activate.sh: never touch compose while a platform deployment is running.
    "exec 9>/opt/cmp/deploy.lock; "
    "flock -w 900 9 || { echo 'Another demo deployment is running' >&2; exit 1; }; "
    f"mkdir -p {web}; "
    f"aws s3 cp s3://{bucket}/releases/web-{revision}.tar.gz /tmp/cmp-web.tar.gz --only-show-errors; "
    f"tar -xzf /tmp/cmp-web.tar.gz -C {web}; "
    f"ln -sfn {web} /opt/cmp/web/current; "
    "install -d -m 700 -o 1000 -g 1000 /opt/cmp/data/web-models /opt/cmp/data/web-cache; "
    "find /opt/cmp/web -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\\n' | sort -rn | tail -n +4 | cut -d' ' -f2- | xargs -r rm -rf; "
    "release=/opt/cmp/releases/$(cat /opt/cmp/current-revision); cd $release; "
    "docker compose --env-file /opt/cmp/.env -f infra/demo/compose.yaml --profile public up -d --build --no-deps web; "
    "sleep 5; docker inspect --format '{{.State.Running}}' cmp-demo-web-1 | grep -q true"
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
