#!/usr/bin/env bash
# 데모용 부하 생성기를 중계 서버에서 켜고 끈다. 이 PC(Git Bash)에서 실행한다. 비밀값은 다루지 않는다.
#
#   bash demo-load.sh start     # 초당 20건을 중계 서버 입구(8080)로 보낸다. 10분 뒤 저절로 꺼진다.
#   bash demo-load.sh stop      # 바로 끈다
#   bash demo-load.sh status    # 켜져 있는지, 지금 가중치와 누적 요청 수
#   bash demo-load.sh fault block 30    # 장애 주입: 대상(온프레)으로 가는 길을 30초 막는다(응답 없음). 자동 해제
#   bash demo-load.sh fault reject 30   #   대상이 바로 연결을 거절한다(즉시 오류)
#   bash demo-load.sh fault loss 30     #   대상으로 가는 패킷 30%가 사라진다(지연·간헐 실패, LOSS=50처럼 비율 지정)
#   bash demo-load.sh fault clear       #   지금 바로 해제 / fault status: 상태 확인
#   bash demo-load.sh bots              # 데모 앱 AI 채팅 에이전트 실행 방법 안내
#
# 왜 필요한가: 트래픽 전환은 단계마다 최소 요청 수(지금 서버 설정은 10건)가 들어와야 다음 단계로 올라간다.
# 데모에는 실제 사용자가 없어서, 전환을 시연하기 직전(또는 전환 단계 화면이 뜨는 때)에 start 한다.
# 요청은 중계 서버 안에서 자기 입구로 보낸다. 가중치대로 원본(AWS)과 대상(온프레)으로 나뉘어 간다.
#
# 준비: aws sso login --profile cmp-aws-prod
set -u
export MSYS_NO_PATHCONV=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8
export AWS_PROFILE="${AWS_PROFILE:-cmp-aws-prod}"
AWS="/c/Program Files/Amazon/AWSCLIV2/aws.exe"
REGION=ap-northeast-2
RELAY_INSTANCE=i-017c5ae4b2c8894d8    # 중계 서버(cmp-demo-relay)
UNIT=demo-load
FAULT_TARGET="${FAULT_TARGET:-10.200.0.4}"   # 장애를 주입할 대상(온프레 앱, 터널 주소)
RATE="${RATE:-20}"                    # 초당 요청 수
MINUTES="${MINUTES:-10}"              # 자동으로 꺼지는 시간
WORK="$(cygpath -m "$(mktemp -d)")"
mode="${1:-}"

relay_cmd() {  # $1=서버에서 실행할 명령 → 출력 (DRY_RUN=1이면 보내지 않고 명령만 보여 준다)
  if [[ -n "${DRY_RUN:-}" ]]; then echo "[DRY_RUN] $1"; return; fi
  python - "$RELAY_INSTANCE" "$1" "$WORK/relay.json" <<'PY'
import json, sys
json.dump({"InstanceIds": [sys.argv[1]], "DocumentName": "AWS-RunShellScript", "TimeoutSeconds": 120,
           "Parameters": {"commands": [sys.argv[2]], "executionTimeout": ["120"]}}, open(sys.argv[3], "w"))
PY
  local id; id=$("$AWS" ssm send-command --region $REGION --cli-input-json "file://$WORK/relay.json" --query Command.CommandId --output text) || return 1
  for _ in $(seq 1 30); do
    sleep 3
    local out; out=$("$AWS" ssm get-command-invocation --region $REGION --command-id "$id" --instance-id $RELAY_INSTANCE --query '[Status,StandardOutputContent]' --output text 2>/dev/null)
    case "$out" in Success*|Failed*) echo "$out" | sed '1s/^[A-Za-z]*\t//'; return;; esac
  done
  echo "(응답 없음: 잠시 뒤 status로 확인하세요)"
}

show_status() {
  relay_cmd "echo -n '부하 생성기: '; systemctl is-active $UNIT 2>/dev/null || echo 꺼짐; echo -n '가중치(원본,대상): '; grep -o '\"weight\": [0-9]*' /etc/envoy/rds/rds.json | tr '\\n' ' '; echo; curl -s -m3 http://127.0.0.1:9901/stats | grep -E 'cluster\\.cluster_(origin|target)\\.upstream_rq_total' | sed 's/cluster.cluster_//'; echo -n '대상: '; curl -s -m3 http://127.0.0.1:9901/clusters | grep -E 'cluster_target::[0-9.]+:[0-9]+::health_flags' | head -1"
}

case "$mode" in
  start)
    relay_cmd "systemctl stop $UNIT 2>/dev/null; systemd-run --unit=$UNIT --property=RuntimeMaxSec=$((MINUTES*60)) bash -c 'while true; do for i in \$(seq 1 $RATE); do curl -s -m5 -o /dev/null http://127.0.0.1:8080/api/messages & done; wait; sleep 1; done' >/dev/null 2>&1; sleep 1; echo -n '부하 생성(초당 ${RATE}건, ${MINUTES}분 뒤 자동 종료): '; systemctl is-active $UNIT"
    ;;
  stop)
    relay_cmd "systemctl stop $UNIT 2>/dev/null; echo -n '부하 생성기: '; systemctl is-active $UNIT 2>/dev/null || echo 꺼짐"
    ;;
  status) show_status ;;
  fault)
    kind="${2:-}"; secs="${3:-30}"
    [[ "$secs" =~ ^[0-9]+$ ]] || { echo "초는 숫자로 주세요(예: fault block 30)"; exit 2; }
    (( secs > 300 )) && secs=300   # 안전: 한 번에 최대 5분. 시간이 지나면 저절로 풀린다.
    T="$FAULT_TARGET"
    # 이 스크립트가 만든 규칙에만 comment=demo-fault를 붙여, 정리할 때 그것만 지운다.
    CLEAR='for ch in OUTPUT FORWARD; do while iptables -S $ch | grep -q demo-fault; do r=$(iptables -S $ch | grep demo-fault | head -1 | sed "s/^-A/-D/"); iptables $r || break; done; done'
    CLEAR_SD="${CLEAR//\$/\$\$}"   # systemd는 인자의 $를 변수로 풀기 때문에 $$로 두 번 쓴다
    show_fault='echo -n "장애 주입 규칙: "; (iptables -S OUTPUT; iptables -S FORWARD) | grep -c demo-fault; echo -n "대상 헬스: "; curl -s http://127.0.0.1:9901/clusters | grep -o "cluster_target::[^ ]*health_flags::[^ ]*" | head -1'
    case "$kind" in
      block|reject|loss)
        case "$kind" in
          block)  ACT="-j DROP" ;;                                  # 응답이 없음(시간 초과) → 헬스 체크 실패
          reject) ACT="-j REJECT --reject-with tcp-reset" ;;        # 바로 거절(연결 거부) → 즉시 오류
          loss)   [[ "${LOSS:-30}" =~ ^[0-9]+$ ]] && (( LOSS > 0 && LOSS < 100 )) 2>/dev/null || [[ -z "${LOSS:-}" ]] || { echo "LOSS는 1~99"; exit 2; }
                  ACT="-m statistic --mode random --probability 0.$(printf '%02d' "${LOSS:-30}") -j DROP" ;;  # 패킷 일부 손실 → 지연·간헐 실패
        esac
        relay_cmd "$CLEAR; systemctl stop demo-fault-clear.timer demo-fault-clear.service 2>/dev/null; for ch in OUTPUT FORWARD; do iptables -I \$ch -d $T -p tcp --dport 8080 -m comment --comment demo-fault $ACT; done; systemd-run --unit=demo-fault-clear --on-active=${secs}s bash -c '$CLEAR_SD' >/dev/null 2>&1; echo '장애 주입: ${kind} → 대상 ${T}:8080, ${secs}초 뒤 자동 해제'; $show_fault"
        ;;
      clear) relay_cmd "$CLEAR; systemctl stop demo-fault-clear.timer demo-fault-clear.service 2>/dev/null; echo '장애 주입 해제'; $show_fault" ;;
      status) relay_cmd "$show_fault" ;;
      *) echo "사용법: bash demo-load.sh fault <block|reject|loss|clear|status> [초(기본 30, 최대 300)]"; exit 2 ;;
    esac
    ;;
  bots)
    # 데모 앱의 AI 채팅 에이전트(demo-app 저장소 agents/chatbots.py)를 이 PC에서 돌린다.
    # 중계 서버 입구로 보내므로 전환 중 메시지의 app_env·db_node가 바뀌는 것이 채팅창에 그대로 보인다.
    echo "demo-app 저장소의 agents 폴더에서 실행하세요(키 없이는 --offline):"
    echo "  uv run chatbots.py --url http://15.164.0.37:8080            # Claude로 대화(ANTHROPIC_API_KEY 필요)"
    echo "  uv run chatbots.py --url http://15.164.0.37:8080 --offline  # 준비된 대사로 대화"
    echo "참고: 에이전트 발언은 초당 0.2건 안팎이라 전환 단계의 최소 요청 수(10건)를 혼자 채우지 못합니다. start로 부하도 같이 켜세요."
    ;;
  *) sed -n 2,16p "$0"; exit 2 ;;
esac
rm -rf "$WORK" 2>/dev/null
