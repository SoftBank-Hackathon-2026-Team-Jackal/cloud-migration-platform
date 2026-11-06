#!/usr/bin/env bash
# 2안 PoC 정리(티켓 69). NLB를 먼저 지우고 AWS에서 사라진 것을 확인한 뒤 Namespace를 지운다.
source "$(dirname "$0")/lib.sh"
check_account

k3d delete ns "$NS" --ignore-not-found --wait=true
dns="$(eks -n "$NS" get svc wg-endpoint -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"

# Service를 먼저 지워야 LBC가 NLB·대상 그룹·보안 그룹을 지운다(Namespace 먼저 지우면 finalizer 대기)
eks -n "$NS" delete svc wg-endpoint --ignore-not-found --wait=true --timeout=300s
log "NLB·대상 그룹·보안 그룹 삭제 확인"
for _ in $(seq 60); do
  left="$(aws elbv2 describe-load-balancers --query "LoadBalancers[?contains(LoadBalancerName, 'cmpdmpoc') || DNSName=='${dns:-none}'].LoadBalancerArn" --output text)"
  tgs="$(aws elbv2 describe-target-groups --query "TargetGroups[?contains(TargetGroupName, 'cmpdmpoc')].TargetGroupArn" --output text)"
  sgs="$(lb_sgs)"
  [[ -z "$left$tgs$sgs" ]] && break; sleep 10
done
[[ -z "$left$tgs$sgs" ]] || { echo "남은 리소스: $left $tgs $sgs" >&2; exit 1; }
eks delete ns "$NS" --ignore-not-found --wait=true
log "정리 완료(NLB·대상 그룹·보안 그룹 없음, 양쪽 $NS 삭제)"
