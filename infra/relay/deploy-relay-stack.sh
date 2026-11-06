#!/usr/bin/env bash
# 중계 서버 스택(cmp-demo-relay, #200)을 만들거나 갱신한다. 다른 스택은 건드리지 않는다.
#   deploy-relay-stack.sh --plan   # 변경 세트만 만들고 보여 준다(적용하지 않음)
#   deploy-relay-stack.sh          # 스택을 만들거나 갱신한다
# 서버 안 설정(WireGuard·Envoy)은 만든 뒤 setup-relay.sh를 SSM으로 실행한다.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

profile=cmp-aws-prod
region=ap-northeast-2
account=714066696240
control_stack=cmp-demo-control-plane
stack=cmp-demo-relay
mode="${1:-apply}"
[[ "$mode" == apply || "$mode" == --plan ]] || { echo "Use --plan or no argument" >&2; exit 2; }

actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$actual_account" == "$account" ]] || { echo "Unexpected AWS account: $actual_account" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit the reviewed template first" >&2; exit 1; }

# 기존 데모 호스트와 같은 VPC·서브넷(읽기 전용 조회).
param() {
  aws cloudformation describe-stacks --profile "$profile" --region "$region" --stack-name "$control_stack" \
    --query "Stacks[0].Parameters[?ParameterKey==\`$1\`].ParameterValue|[0]" --output text
}
vpc="$(param VpcId)"
subnet="$(param SubnetId)"
[[ "$vpc" == vpc-* && "$subnet" == subnet-* ]] || { echo "Unexpected network parameters" >&2; exit 1; }

extra=()
[[ "$mode" == --plan ]] && extra=(--no-execute-changeset)
aws cloudformation deploy \
  --profile "$profile" --region "$region" \
  --stack-name "$stack" \
  --template-file infra/relay/relay-stack.yaml \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  ${extra[@]+"${extra[@]}"} \
  --parameter-overrides VpcId="$vpc" SubnetId="$subnet" \
  --tags Project=cloud-migration-platform Purpose=hackathon-demo-relay
