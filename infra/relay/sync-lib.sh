#!/usr/bin/env bash
# 중계 서버(cmp-demo-relay)의 /opt/cmp/lib을 이 저장소의 한 커밋으로 맞춘다.
#
# 중계 서버의 relay worker(트래픽 전환 Activity)와 traffic-health는 /opt/cmp/lib의 코드를 쓴다. 이 경로는 데모 서버
# 배포(release.sh)에 들어 있지 않아서 손으로 갱신해 왔고, 데모 서버와 중계 서버의 트래픽 코드가 어긋났다
# (2026-11-07: cmp_worker/activities/traffic.py가 11/6 버전). 이 스크립트가 그 갱신을 한 번에 같은 방식으로 한다.
#
#   bash infra/relay/sync-lib.sh            # origin/main
#   bash infra/relay/sync-lib.sh <ref>      # 다른 커밋(로컬 확인용)
#   FORCE=1 bash infra/relay/sync-lib.sh    # 실행 중인 트래픽 전환이 있어도 진행
#
# 순서: 실행 중 트래픽 전환 확인(데모 서버 Temporal) → 코드 묶음(cmp_traffic + relay worker가 쓰는 cmp_worker 파일)을
# SSM으로 보냄 → 기존 디렉터리 백업 → 풀기 → python3.11 import 확인 → 실패하면 백업으로 되돌림 → 서비스 재시작.
# 중계 서버는 배포 버킷을 읽지 않으므로 묶음을 SSM 명령에 base64로 싣는다(약 60KB). 비밀값은 싣지 않는다.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
REGION=${AWS_REGION:-ap-northeast-2}
RELAY_NAME=${RELAY_NAME:-cmp-demo-relay}
DEMO_STACK=${DEMO_STACK:-cmp-demo-control-plane}
cd "$ROOT"

if [ "${1:-}" ]; then
  REF=$1
else
  git fetch -q origin main
  REF=origin/main
fi
REV=$(git rev-parse --short=12 "$REF")

ssm_run() { # instance script-file -> prints stdout, fails on command failure
  local instance=$1 file=$2 params cid status
  params=$(python3 -c 'import json,sys;print(json.dumps({"commands":[open(sys.argv[1]).read()]}))' "$file")
  cid=$(aws ssm send-command --region "$REGION" --instance-ids "$instance" \
    --document-name AWS-RunShellScript --parameters "$params" --query Command.CommandId --output text)
  for _ in $(seq 1 90); do
    sleep 2
    status=$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
      --instance-id "$instance" --query Status --output text 2>/dev/null || true)
    case $status in Success | Failed | TimedOut | Cancelled) break ;; esac
  done
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$instance" \
    --query StandardOutputContent --output text
  [ "$status" = Success ] || {
    aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$instance" \
      --query StandardErrorContent --output text >&2
    echo "SSM command $status" >&2
    return 1
  }
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

relay=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$RELAY_NAME" Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ "$relay" != None ] || { echo "running instance $RELAY_NAME not found" >&2; exit 1; }

if [ -z "${FORCE:-}" ]; then
  demo=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$DEMO_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
  cat > "$work/check.sh" <<'EOF'
curl -s -G http://127.0.0.1:8080/api/v1/namespaces/default/workflows \
  --data-urlencode 'query=ExecutionStatus="Running" AND WorkflowType="TrafficShiftWorkflow"' \
  | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("executions") or []))'
EOF
  running=$(ssm_run "$demo" "$work/check.sh" | tr -d '[:space:]')
  if [ "$running" != 0 ]; then
    echo "refused: $running TrafficShiftWorkflow running (restart would interrupt the shift); FORCE=1 to override" >&2
    exit 1
  fi
fi

# relay worker가 쓰는 파일만: cmp_traffic 전체 + cmp_worker의 relay 경로(workers/relay.py → activities/traffic.py·traffic_shadow.py, config.py)
mkdir -p "$work/lib"
git archive "$REF" packages/traffic/src/cmp_traffic | tar -x -C "$work"
mv "$work/packages/traffic/src/cmp_traffic" "$work/lib/"
for f in __init__.py config.py activities/__init__.py activities/traffic.py activities/traffic_shadow.py workers/__init__.py workers/relay.py; do
  mkdir -p "$work/lib/cmp_worker/$(dirname "$f")"
  git show "$REF:apps/worker/src/cmp_worker/$f" > "$work/lib/cmp_worker/$f"
done
echo "$REV" > "$work/lib/REVISION"
COPYFILE_DISABLE=1 tar -C "$work/lib" -czf "$work/lib.tgz" cmp_traffic cmp_worker REVISION
payload=$(base64 < "$work/lib.tgz" | tr -d '\n')

cat > "$work/apply.sh" <<EOF
set -eu
cd /opt/cmp/lib
stamp=\$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /opt/cmp/lib-backups
tar -czf /opt/cmp/lib-backups/lib-\$stamp.tgz --exclude='*.bak-*' --exclude='._*' cmp_traffic cmp_worker \$(ls REVISION 2>/dev/null)
ls -1t /opt/cmp/lib-backups/lib-*.tgz | tail -n +6 | xargs -r rm -f
new=\$(mktemp -d)
echo '$payload' | base64 -d | tar -xz -C "\$new"
rm -rf cmp_traffic cmp_worker ._cmp_traffic
cp -a "\$new"/cmp_traffic "\$new"/cmp_worker "\$new"/REVISION .
rm -rf "\$new"
if ! PYTHONPATH=/opt/cmp/lib /usr/bin/python3.11 -c 'import cmp_worker.workers.relay, cmp_traffic.health' 2>/tmp/cmp-lib-import.err; then
  echo "import failed; restoring \$stamp" >&2; cat /tmp/cmp-lib-import.err >&2
  rm -rf cmp_traffic cmp_worker REVISION
  tar -xzf /opt/cmp/lib-backups/lib-\$stamp.tgz
  exit 1
fi
systemctl restart relay-worker traffic-health
sleep 3
systemctl is-active relay-worker traffic-health
echo "relay lib now \$(cat REVISION)"
EOF
echo "sync $RELAY_NAME ($relay) /opt/cmp/lib -> $REV"
ssm_run "$relay" "$work/apply.sh"
