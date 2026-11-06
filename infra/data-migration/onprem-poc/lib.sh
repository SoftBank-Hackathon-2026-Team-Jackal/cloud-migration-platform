# 2안 PoC 공통값(티켓 69). up.sh·check.sh·down.sh가 source 한다.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../../.." && pwd)"
NS=cmp-dm-poc
ACCOUNT=714066696240
CLUSTER=cmp-demo-source
export AWS_PROFILE="${AWS_PROFILE:-cmp-aws-prod}" AWS_REGION="${AWS_REGION:-ap-northeast-2}"
EKS_CONTEXT="${EKS_CONTEXT:-minseo.kim@cmp-demo-source.ap-northeast-2.eksctl.io}"
K3D_CONTEXT="${K3D_CONTEXT:-k3d-cmp-local}"
STACK="$NS/wg-endpoint"  # AWS Load Balancer Controller가 붙이는 service.k8s.aws/stack 태그 값

eks() { kubectl --context "$EKS_CONTEXT" "$@"; }
k3d() { kubectl --context "$K3D_CONTEXT" "$@"; }
log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

check_account() {
  local got; got="$(aws sts get-caller-identity --query Account --output text)"
  [[ "$got" == "$ACCOUNT" ]] || { echo "AWS 계정이 $ACCOUNT 가 아닙니다($got)" >&2; exit 1; }
}

py() { (cd "$root" && uv run --quiet python "$@"); }

# Secret wg-key가 없으면 비밀키를 만들어 stdin으로 넣는다. 공개키만 stdout에 낸다(비밀키는 화면·인자에 없음).
ensure_key() {  # <kubectl fn>
  local kc="$1"
  if ! "$kc" -n "$NS" get secret wg-key >/dev/null 2>&1; then
    py -c '
import base64, json, sys
from cmp_providers.agent_tunnel import generate_private_key
priv = base64.b64encode(generate_private_key())
json.dump({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": "wg-key", "namespace": sys.argv[1],
                        "labels": {"app.kubernetes.io/managed-by": "cmp-onprem-poc"}},
           "data": {"private": base64.b64encode(priv).decode()}}, sys.stdout)
' "$NS" | "$kc" apply -f - >/dev/null
  fi
  "$kc" -n "$NS" get secret wg-key -o jsonpath='{.data.private}' | py -c '
import base64, sys
from cmp_providers.agent_tunnel import public_key
print(base64.b64encode(public_key(base64.b64decode(base64.b64decode(sys.stdin.read())))).decode())
'
}

# LBC가 만든 NLB 보안 그룹(정리 확인용)
lb_sgs() {
  aws ec2 describe-security-groups --filters "Name=tag:service.k8s.aws/stack,Values=$STACK" \
    --query 'SecurityGroups[].GroupId' --output text
}
