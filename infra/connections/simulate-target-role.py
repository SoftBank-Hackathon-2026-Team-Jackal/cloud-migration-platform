"""Ask IAM whether the target connection role template allows what Terraform needs (ticket 47).

Read-only: `iam simulate-custom-policy` evaluates the rendered TargetBuild policy document; no
role, stack or resource is created. Cases come from `cmp_providers.aws_target_access` (the same
list `tests/test_aws_target_access.py` evaluates offline). Exit code 1 if any answer differs.

    uv run python infra/connections/simulate-target-role.py [--profile P] [--template FILE]

The simulator applies the given context to the request; which keys AWS really sends for a new
resource cannot be simulated, so the cases use the strict reading (see the module docstring).
SCP and permission boundaries of the user's account are not part of this check.
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from cmp_providers.aws_target_access import allowed_cases, refusals, render_target_policies

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT, REGION, NAME = "111122223333", "ap-northeast-2", "cmp-demo"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/cmp-target-ConnectionRole-SIMULATED"


def _entries(context: dict[str, str]) -> list[str]:
    return [
        f"ContextKeyName={key},ContextKeyValues={value},ContextKeyType=string"
        for key, value in sorted(context.items())
    ]


def simulate(
    profile: str, policies: list[str], actions: list[str], resource: str, context: dict
) -> dict:
    command = [
        "aws", "iam", "simulate-custom-policy", "--profile", profile, "--output", "json",
        "--policy-input-list", *policies, "--action-names", *actions,
    ]  # fmt: skip
    if resource != "*":
        command += ["--resource-arns", resource]
    if context:
        command += ["--context-entries", *_entries(context)]
    result = json.loads(subprocess.run(command, capture_output=True, check=True).stdout)
    return {r["EvalActionName"]: r["EvalDecision"] for r in result["EvaluationResults"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="cmp-aws-prod")
    parser.add_argument(
        "--template", type=Path, default=ROOT / "infra/connections/target-role.yaml"
    )
    args = parser.parse_args()
    template = args.template.read_text()
    documents = render_target_policies(template, account=ACCOUNT, role_arn=ROLE_ARN)
    policies = [json.dumps(document) for document in documents]
    expected: dict[tuple, dict[str, tuple[str, str]]] = defaultdict(dict)
    for case in allowed_cases(ACCOUNT, REGION, NAME):
        key = (case.resource, json.dumps(case.context, sort_keys=True))
        expected[key][case.action] = ("allowed", case.label)
    for refusal in refusals(ACCOUNT, REGION, NAME):
        key = (refusal.resource, json.dumps(refusal.context, sort_keys=True))
        expected[key][refusal.action] = ("denied", refusal.why)
    failures = total = 0
    for (resource, context), actions in expected.items():
        decisions = simulate(args.profile, policies, sorted(actions), resource, json.loads(context))
        for action, (want, label) in sorted(actions.items()):
            total += 1
            got = decisions[action]
            ok = (got == "allowed") == (want == "allowed")
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {want:7} {got:13} {label}")
    print(f"{total - failures}/{total} as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
