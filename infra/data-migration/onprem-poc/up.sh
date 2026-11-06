#!/usr/bin/env bash
# 2안 PoC 배치(티켓 69): 원본 EKS에 게이트웨이 + 인터넷용 UDP NLB(허용 CIDR만), 맥 k3d에 접속하는 게이트웨이.
# 원본 EKS·AWS를 바꾼다. 사용자 승인 뒤에만 CONFIRM_AWS=yes 로 실행한다. 정리는 down.sh.
#   SOURCE_CIDR  NLB에 들어올 수 있는 주소(기본: 지금 맥 공인 IP /32)
source "$(dirname "$0")/lib.sh"
[[ "${CONFIRM_AWS:-}" == yes ]] || { echo "원본 EKS·AWS를 바꿉니다. 승인 뒤 CONFIRM_AWS=yes 로 실행하세요" >&2; exit 2; }
check_account
SOURCE_CIDR="${SOURCE_CIDR:-$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')/32}"
log "허용 CIDR $SOURCE_CIDR"

for kc in eks k3d; do
  "$kc" create ns "$NS" --dry-run=client -o yaml | "$kc" apply -f - >/dev/null
  "$kc" label ns "$NS" app.kubernetes.io/managed-by=cmp-onprem-poc --overwrite >/dev/null
done
k3d_pub="$(ensure_key k3d)"
eks_pub="$(ensure_key eks)"

py "$here/poc.py" eks --peer-public-key "$k3d_pub" --source-cidr "$SOURCE_CIDR" | eks apply -f - >/dev/null
eks -n "$NS" rollout status deploy/wg-gateway --timeout=300s

log "NLB 주소 대기"
for _ in $(seq 60); do
  lb="$(eks -n "$NS" get svc wg-endpoint -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
  [[ -n "$lb" ]] && break; sleep 5
done
[[ -n "${lb:-}" ]] || { eks -n "$NS" describe svc wg-endpoint >&2; echo "NLB 주소가 나오지 않았습니다" >&2; exit 1; }
log "NLB $lb"

log "대상 그룹 healthy 대기"
arn="$(aws elbv2 describe-load-balancers --query "LoadBalancers[?DNSName=='$lb'].LoadBalancerArn" --output text)"
tg="$(aws elbv2 describe-target-groups --load-balancer-arn "$arn" --query 'TargetGroups[0].TargetGroupArn' --output text)"
for _ in $(seq 60); do
  state="$(aws elbv2 describe-target-health --target-group-arn "$tg" --query 'TargetHealthDescriptions[0].TargetHealth.State' --output text)"
  [[ "$state" == healthy ]] && break; sleep 10
done
log "대상 상태 $state"
log "DNS 대기"
for _ in $(seq 60); do dig +short "$lb" | grep -q . && break; sleep 5; done

py "$here/poc.py" k3d --peer-public-key "$eks_pub" --endpoint "$lb:51820" | k3d apply -f - >/dev/null
k3d -n "$NS" rollout status deploy/wg-gateway --timeout=300s
log "배치 완료. 다음: check.sh"
