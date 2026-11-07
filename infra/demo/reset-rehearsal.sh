#!/usr/bin/env bash
# 시연 리허설 사이에 이전 상태를 처음으로 되돌린다(AWS EKS 원본 → 온프레 K3s 대상, ingress=source 터널).
#
# 이전이 끝나거나 멈춘 뒤 남는 것을 다음 실행이 물려받아 결과가 달라졌다(2026-11-07):
# 끝나지 않는 DataMigrationWorkflow, PgBouncer로 바뀐 원본 앱 DB 주소, 원본·대상 복제 객체, 대상 DB의 표
# (TARGET_SCHEMA_PARTIAL), 실행별 터널 리소스, 중계 서버의 대상 100% 가중치(TRAFFIC_BLOCKED),
# Namespace를 다시 만들며 사라진 사전 점검 Secret(NETWORK_PREFLIGHT_UNKNOWN).
#
#   SOURCE_CONTEXT=<원본 EKS kube context> TARGET_CONTEXT=<온프레 kube context> bash infra/demo/reset-rehearsal.sh
#
# 원본 앱의 DB 주소는 cmp-cutover Secret에 저장된 원래 값으로 되돌린다. 전환 뒤 대상 DB에만 쓰인 행은 원본으로
# 돌아오지 않는다(리허설용). 값은 출력하지 않는다. 원본 앱 Namespace·이름은 아래 변수로 바꾼다.
set -euo pipefail

: "${SOURCE_CONTEXT:?SOURCE_CONTEXT (source EKS kube context) is required}"
: "${TARGET_CONTEXT:?TARGET_CONTEXT (on-prem kube context) is required}"
REGION=${AWS_REGION:-ap-northeast-2}
DEMO_STACK=${DEMO_STACK:-cmp-demo-control-plane}
RELAY_NAME=${RELAY_NAME:-cmp-demo-relay}
SRC_NS=${SOURCE_NAMESPACE:-chat-source}
SRC_APP=${SOURCE_APP:-chat-app}
SRC_DB_POD=${SOURCE_DB_POD:-chat-db-0}
SRC_URL_SECRET=${SOURCE_URL_SECRET:-chat-secrets}
SRC_URL_KEY=${SOURCE_URL_KEY:-DATABASE_URL}
TGT_NS=${TARGET_NAMESPACE:-cmp-app}
TGT_DB_POD=${TARGET_DB_POD:-chat-db-0}
PREFLIGHT_SECRET=${PREFLIGHT_SECRET:-cmp-preflight-source-db}
TUNNEL_NS=cmp-data-tunnel
ROOT=$(cd "$(dirname "$0")/../.." && pwd)

src() { kubectl --context "$SOURCE_CONTEXT" "$@"; }
tgt() { kubectl --context "$TARGET_CONTEXT" "$@"; }
psql_in() { # context namespace pod  (SQL on stdin)
  kubectl --context "$1" -n "$2" exec -i "$3" -- \
    sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-chat}" -At'
}
ssm_run() { # instance script-file
  local params cid status
  params=$(python3 -c 'import json,sys;print(json.dumps({"commands":[open(sys.argv[1]).read()]}))' "$2")
  cid=$(aws ssm send-command --region "$REGION" --instance-ids "$1" --document-name AWS-RunShellScript \
    --parameters "$params" --query Command.CommandId --output text)
  for _ in $(seq 1 60); do
    sleep 2
    status=$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$1" \
      --query Status --output text 2>/dev/null || true)
    case $status in Success | Failed | TimedOut | Cancelled) break ;; esac
  done
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$1" \
    --query StandardOutputContent --output text
  [ "$status" = Success ]
}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

demo=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$DEMO_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
relay=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$RELAY_NAME" Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)

echo "1) 남은 데이터 이전 워크플로 terminate(신호를 보내지 않는다: rollback·finish는 대상 DB를 건드린다)"
cat > "$work/wf.sh" <<'EOF'
api=http://127.0.0.1:8080/api/v1/namespaces/default/workflows
addr=$(docker exec cmp-demo-temporal-1 hostname -i | cut -d' ' -f1):7233
for w in $(curl -s -G "$api" --data-urlencode 'query=ExecutionStatus="Running" AND WorkflowType="DataMigrationWorkflow"' \
  | python3 -c 'import sys,json; [print(e["execution"]["workflowId"]) for e in json.load(sys.stdin).get("executions") or []]'); do
  docker exec cmp-demo-temporal-1 temporal workflow terminate --address "$addr" -w "$w" --reason rehearsal-reset >/dev/null
  echo "  terminated $w"
done
EOF
ssm_run "$demo" "$work/wf.sh"

echo "2) 원본 앱 DB 주소를 원래 값으로"
if src -n "$SRC_NS" get secret cmp-cutover >/dev/null 2>&1; then
  orig=$(src -n "$SRC_NS" get secret cmp-cutover -o jsonpath='{.data.original_url}')
  cur=$(src -n "$SRC_NS" get secret "$SRC_URL_SECRET" -o jsonpath="{.data.$SRC_URL_KEY}")
  if [ -n "$orig" ] && [ "$orig" != "$cur" ]; then
    src -n "$SRC_NS" patch secret "$SRC_URL_SECRET" --type merge -p "{\"data\":{\"$SRC_URL_KEY\":\"$orig\"}}" >/dev/null
    src -n "$SRC_NS" rollout restart "deploy/$SRC_APP" >/dev/null
    src -n "$SRC_NS" rollout status "deploy/$SRC_APP" --timeout=180s >/dev/null
    echo "  restored $SRC_URL_SECRET.$SRC_URL_KEY and restarted $SRC_APP"
  fi
fi

drop_replication() { # context namespace pod
  psql_in "$1" "$2" "$3" >/dev/null <<'SQL'
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT subname FROM pg_subscription WHERE subname LIKE 'cmp\_%' LOOP
    EXECUTE format('ALTER SUBSCRIPTION %I DISABLE', r.subname);
    EXECUTE format('ALTER SUBSCRIPTION %I SET (slot_name = NONE)', r.subname);
    EXECUTE format('DROP SUBSCRIPTION %I', r.subname);
  END LOOP;
  FOR r IN SELECT pubname FROM pg_publication WHERE pubname LIKE 'cmp\_%' LOOP
    EXECUTE format('DROP PUBLICATION %I', r.pubname);
  END LOOP;
  FOR r IN SELECT slot_name FROM pg_replication_slots WHERE slot_name LIKE 'cmp\_%' AND NOT active LOOP
    PERFORM pg_drop_replication_slot(r.slot_name);
  END LOOP;
END $$;
SQL
}

echo "3) 원본 DB 복제 객체와 전환 리소스 삭제"
drop_replication "$SOURCE_CONTEXT" "$SRC_NS" "$SRC_DB_POD"
src -n "$SRC_NS" delete deployment/cmp-cutover service/cmp-pgbouncer secret/cmp-cutover \
  networkpolicy/cmp-allow-chat-app-to-cutover --ignore-not-found >/dev/null
for s in $(src -n "$SRC_NS" get secret -o name | grep -E '^secret/cmp-.*-rev$' || true); do
  src -n "$SRC_NS" delete "$s" >/dev/null
done

echo "4) 실행별 터널 리소스 삭제(고정 NLB·게이트웨이는 남기고 쉬는 상태로)"
for r in $(src -n "$TUNNEL_NS" get svc -o name 2>/dev/null | grep -E '/cmp-wg-.*-peer-db$' | grep -v standing || true); do
  src -n "$TUNNEL_NS" delete "$r" >/dev/null
done
for kind in deployment service configmap secret; do
  for r in $(tgt -n "$TUNNEL_NS" get "$kind" -o name 2>/dev/null | grep -E '/cmp-wg-' || true); do
    tgt -n "$TUNNEL_NS" delete "$r" >/dev/null
  done
done
for r in $(tgt -n "$TGT_NS" get networkpolicy -o name 2>/dev/null | grep -E '/cmp-wg-.*-db-in$' || true); do
  tgt -n "$TGT_NS" delete "$r" >/dev/null
done
if src -n "$TUNNEL_NS" get svc cmp-wg-standing-endpoint >/dev/null 2>&1; then
  # 워커와 같은 field manager로 쓴다(kubectl annotate/patch는 다음 실행의 server-side apply와 충돌한다).
  ranges=$(src -n "$TUNNEL_NS" get svc cmp-wg-standing-endpoint -o jsonpath='{.spec.loadBalancerSourceRanges}')
  (cd "$ROOT" && uv run -q python -c \
    'import json,sys; from cmp_data import tunnel as wg; print(json.dumps(wg.standing_endpoint_manifest(json.loads(sys.argv[1]))))' \
    "$ranges") | src apply --server-side --field-manager cmp-data-migration --force-conflicts -f - >/dev/null
fi

echo "5) 대상 DB 비우기(복제 객체, public 표와 시퀀스)"
drop_replication "$TARGET_CONTEXT" "$TGT_NS" "$TGT_DB_POD"
psql_in "$TARGET_CONTEXT" "$TGT_NS" "$TGT_DB_POD" >/dev/null <<'SQL'
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.tablename);
  END LOOP;
  -- 주인 표가 없는 시퀀스(예: 덤프에 따로 들어온 cmp_switchover_marker_id_seq)는 표를 지워도 남아서
  -- 다음 스키마 복원을 "already exists"로 멈춘다.
  FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
    EXECUTE format('DROP SEQUENCE IF EXISTS public.%I CASCADE', r.sequencename);
  END LOOP;
END $$;
SQL

echo "6) 사전 점검 Secret(원본 DB 읽기 계정)이 대상 Namespace에 있는지"
if ! tgt -n "$TGT_NS" get secret "$PREFLIGHT_SECRET" >/dev/null 2>&1; then
  src -n "$SRC_NS" get secret "$SRC_URL_SECRET" -o jsonpath="{.data.$SRC_URL_KEY}" | base64 -d | python3 -c '
import json, sys, urllib.parse
u = urllib.parse.urlparse(sys.stdin.read().strip())
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
  "metadata": {"name": sys.argv[1], "namespace": sys.argv[2]},
  "stringData": {"username": urllib.parse.unquote(u.username or ""),
                 "password": urllib.parse.unquote(u.password or "")}}))' "$PREFLIGHT_SECRET" "$TGT_NS" \
    | tgt apply -f - >/dev/null
  echo "  created $TGT_NS/$PREFLIGHT_SECRET"
fi

echo "7) 중계 서버 가중치 원본 100%"
cat > "$work/relay.sh" <<'EOF'
cd /tmp && PYTHONPATH=/opt/cmp/lib /usr/bin/python3.11 - <<'PY'
import os
from pathlib import Path
from cmp_traffic import weights_for
from cmp_traffic.envoy import EnvoyProxy
import cmp_worker.activities.traffic as t
os.environ.setdefault("TRAFFIC_STREAM_PREFIXES", "/api/stream")
proxy = EnvoyProxy(Path("/etc/envoy/rds/rds.json"), "http://127.0.0.1:9901",
                   cds_path=Path("/etc/envoy/cds/cds.json"), stream_prefixes=t.stream_prefixes_from_env())
before = proxy.target_weight()
if before != 0:
    proxy.apply(weights_for(0))
print(f"  relay target weight {before} -> {proxy.target_weight()}")
PY
EOF
ssm_run "$relay" "$work/relay.sh"

echo "8) 확인"
echo "  source $SRC_APP ready: $(src -n "$SRC_NS" get deploy "$SRC_APP" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}')"
echo "  source pub/sub: $(echo "select count(*) from pg_publication; select count(*) from pg_subscription;" | psql_in "$SOURCE_CONTEXT" "$SRC_NS" "$SRC_DB_POD" | tr '\n' ' ')"
echo "  target public tables/sequences: $(echo "select count(*) from pg_tables where schemaname='public'; select count(*) from pg_sequences where schemaname='public';" | psql_in "$TARGET_CONTEXT" "$TGT_NS" "$TGT_DB_POD" | tr '\n' ' ')"
