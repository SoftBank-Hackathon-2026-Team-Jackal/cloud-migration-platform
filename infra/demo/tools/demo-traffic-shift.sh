#!/usr/bin/env bash
# 트래픽 전환 라이브 데모(AWS 원본 → 온프레). 이 PC(Git Bash)에서 실행한다. 비밀값은 다루지 않는다.
#
#   bash demo-traffic-shift.sh forward    # 부하를 걸고 10→50→100%까지 전환(끝나면 100% 유지)
#   bash demo-traffic-shift.sh rollback   # 50% 단계에서 취소 신호 → 원본 100%로 되돌림(자동 롤백 시연)
#   bash demo-traffic-shift.sh status     # 지금 가중치·누적 요청 수·워커 상태만 본다
#   bash demo-traffic-shift.sh reset      # 부하를 끄고 원본 100%로 되돌린다(전환 뒤 정리용)
#
# 준비: aws sso login --profile cmp-aws-prod (AWS CLI 경로는 아래). 전환 중에는 사용자가 없는 서버라 안전하다.
# 걸리는 시간: forward 약 2분 20초, rollback 약 1분 50초.
set -u
export MSYS_NO_PATHCONV=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8
export AWS_PROFILE="${AWS_PROFILE:-cmp-aws-prod}"
AWS="/c/Program Files/Amazon/AWSCLIV2/aws.exe"
REGION=ap-northeast-2
DEMO_INSTANCE=i-091464203346381b5     # 데모 서버(Temporal·워커)
RELAY_INSTANCE=i-017c5ae4b2c8894d8    # 중계 서버(Envoy)
RELAY_URL=http://15.164.0.37:8080
TARGET_URL="${TARGET_URL:-http://10.200.0.4:8080}"     # 릴레이 Envoy의 대상(온프레 Agent의 터널 주소). 환경변수로 바꿀 수 있다
P99_FLOOR_MS="${P99_FLOOR_MS:-500}"      # 응답시간 하한(ms). 서버 .env의 WEB_START_TRAFFIC과 같은 값으로 맞춘다
WORK="$(cygpath -m "$(mktemp -d)")"   # Windows aws가 읽을 수 있는 경로
mode="${1:-}"

run_ssm() {   # $1=instance  $2=JSON 파일 → command id
  "$AWS" ssm send-command --region $REGION --cli-input-json "file://$2" --query Command.CommandId --output text
}
ssm_out() {   # $1=command id  $2=instance
  "$AWS" ssm get-command-invocation --region $REGION --command-id "$1" --instance-id "$2" --query '[Status,StandardOutputContent]' --output text 2>/dev/null
}
mkjson() {    # $1=instance  $2=shell command  $3=outfile
  python - "$1" "$2" "$3" <<'PY'
import json,sys
json.dump({"InstanceIds":[sys.argv[1]],"DocumentName":"AWS-RunShellScript","TimeoutSeconds":900,"Parameters":{"commands":[sys.argv[2]],"executionTimeout":["900"]}},open(sys.argv[3],"w"))
PY
}
relay_cmd() { # $1=shell command → 출력
  mkjson $RELAY_INSTANCE "$1" "$WORK/relay.json"
  local id; id=$(run_ssm $RELAY_INSTANCE "$WORK/relay.json")
  for _ in $(seq 1 30); do
    sleep 3; local out; out=$(ssm_out "$id" $RELAY_INSTANCE); case "$out" in Success*|Failed*) echo "$out" | sed '1s/^[A-Za-z]*	//'; return;; esac
  done
}
show_status() {
  echo "── 지금 상태 ─────────────────────────────"
  relay_cmd "echo -n 'Envoy 가중치(원본,대상): '; grep -o '\"weight\": [0-9]*' /etc/envoy/rds/rds.json | tr '\\n' ' '; echo; curl -s -m3 http://127.0.0.1:9901/stats | grep -E 'cluster\\.cluster_(origin|target)\\.upstream_rq_total' | sed 's/cluster.cluster_//'; echo -n '워커: '; systemctl is-active relay-worker; echo -n '대상 상태: '; curl -s -m3 http://127.0.0.1:9901/clusters | grep -E 'cluster_target::.*health_flags' | head -1"
  echo "릴레이로 호출: $(curl -s -m8 -o /dev/null -w '%{http_code}' $RELAY_URL/api/messages) (공개 입구 $RELAY_URL)"
}
start_load() {
  relay_cmd "systemctl stop demo-load 2>/dev/null; systemd-run --unit=demo-load --property=RuntimeMaxSec=600 bash -c 'while true; do for i in \$(seq 1 20); do curl -s -m5 -o /dev/null http://127.0.0.1:8080/api/messages & done; wait; sleep 1; done' >/dev/null 2>&1; sleep 1; echo -n '부하 생성(초당 20건): '; systemctl is-active demo-load"
}
stop_load() { relay_cmd "systemctl stop demo-load 2>/dev/null; echo '부하 중지'" ; }

shift_run() {  # $1=forward|rollback
  local cancel_at="None"; [ "$1" = rollback ] && cancel_at=50
  local py
  py=$(cat <<PYEOF
import asyncio, os, time
from temporalio.client import Client
CANCEL_AT=$cancel_at
async def main():
    c = await Client.connect(os.environ.get("TEMPORAL_ADDRESS","temporal:7233"))
    wid = "traffic-demo-" + str(int(time.time()))
    req = {"relay_queue":"relay-1",
      "handover":{"status":"READY","ready":True,"external_endpoint":"$TARGET_URL","health_path":"/healthz","port":8080},
      "policy":{"steps":[10,50,100],"observe_seconds":20,"max_step_seconds":120,"min_requests":10,"max_failed_requests":0,"p99_floor_ms":$P99_FLOOR_MS}}
    h = await c.start_workflow("TrafficShiftWorkflow", req, id=wid, task_queue="migration")
    print("workflow:", wid, flush=True)
    last=None; signalled=False
    while True:
        try: st = await h.query("status")
        except Exception: st={"snapshot":{}}
        s = st.get("snapshot") or {}
        line=(s.get("phase"), s.get("step"), s.get("weights"))
        if line!=last and s.get("phase"):
            w=s.get("weights") or {}
            print(time.strftime("%H:%M:%S"), "단계 %s%%  %-9s 원본 %3s%% / 대상 %3s%%" % (s.get("step"), s.get("phase"), w.get("origin"), w.get("target")), flush=True); last=line
        if CANCEL_AT and not signalled and s.get("step")==CANCEL_AT and s.get("phase")=="observing":
            await h.signal("request_cancel"); signalled=True; print(time.strftime("%H:%M:%S"), ">>> 취소 신호(문제 발생 가정): 원본으로 되돌리는 중", flush=True)
        d = await h.describe()
        if d.status is not None and d.status.name!="RUNNING": break
        await asyncio.sleep(2)
    r = await h.result()
    print("결과:", r["status"], "최종 가중치", r["final_weights"], flush=True)
asyncio.run(main())
PYEOF
)
  local b64; b64=$(printf '%s' "$py" | base64 -w0)
  mkjson $DEMO_INSTANCE "echo $b64 | base64 -d | docker exec -i cmp-demo-worker-1 python - 2>&1" "$WORK/shift.json"
  local id; id=$(run_ssm $DEMO_INSTANCE "$WORK/shift.json"); local seen=0
  for _ in $(seq 1 120); do
    sleep 3
    local raw status out; raw=$(ssm_out "$id" $DEMO_INSTANCE); status=$(echo "$raw" | head -1 | cut -f1); out=$(echo "$raw" | tail -n +1 | sed '1s/^[A-Za-z]*\t//')
    local n; n=$(printf '%s\n' "$out" | wc -l)
    if [ "$n" -gt "$seen" ]; then printf '%s\n' "$out" | tail -n +"$((seen+1))" | sed '/^$/d'; seen=$n; fi
    case "$status" in Success|Failed|Cancelled|TimedOut) break;; esac
  done
}

case "$mode" in
  status)   show_status ;;
  forward)  show_status; start_load; echo "── 전환 시작 ─────────────────────────────"; shift_run forward; show_status
            echo "※ 대상 100%로 두었습니다. 원본으로 되돌리려면: bash $0 reset" ;;
  rollback) show_status; start_load; echo "── 전환 시작(50%에서 롤백 시연) ──────────"; shift_run rollback; stop_load; show_status ;;
  reset)    stop_load
            # 원본 100%로: 취소 신호 방식의 전환을 한 번 돌려 롤백 Activity를 쓴다
            echo "── 원본 100%로 복구 ────────────────────"; start_load; shift_run rollback; stop_load; show_status ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
rm -rf "$WORK" 2>/dev/null
