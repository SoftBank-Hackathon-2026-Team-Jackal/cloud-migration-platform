"""Opt-in real-Terraform check for ticket 53 (#188): apply, reviewed destroy and timeouts.

Local backend only, built-in `terraform_data` resources (no provider download, no cloud call).
Uses the same code the Worker uses: `terraform_process.run_graceful` (SIGTERM → grace → SIGKILL)
and `cloud_destroy.destroy_addresses` (only-delete review). Steps:

1. apply two resources → State has 2.
2. `plan -destroy` → review lists exactly those 2 deletes → apply that Plan → State empty.
3. Timeout with grace: apply whose second resource runs `sleep 60`; the deadline sends SIGTERM →
   Terraform exits gracefully: lock released, the in-flight resource is in the State.
4. Timeout without grace (SIGKILL only, the pre-53 behavior): lock file left, the in-flight
   resource missing from the State.
5. Clean-up: remove the stale local lock (what `force-unlock` does), destroy, State empty.

Usage: uv run python scripts/smoke_terraform_interrupt.py --terraform /path/to/terraform
       [--evidence docs/verification/provisioning-53-terraform.json]
"""

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from cmp_providers.cloud_destroy import destroy_addresses
from cmp_providers.terraform_process import run_graceful

MAIN_TF = """
variable "slow_seconds" { default = 0 }
resource "terraform_data" "first" { input = "first" }
resource "terraform_data" "slow" {
  depends_on = [terraform_data.first]
  input      = var.slow_seconds
  provisioner "local-exec" { command = "sleep ${var.slow_seconds}" }
}
"""
LOCK = ".terraform.tfstate.lock.info"


def state_names(work: Path) -> list[str]:
    path = work / "terraform.tfstate"
    if not path.exists():
        return []
    return sorted(r["name"] for r in json.loads(path.read_text()).get("resources", []))


def tainted(work: Path) -> list[str]:
    path = work / "terraform.tfstate"
    if not path.exists():
        return []
    return sorted(
        r["name"]
        for r in json.loads(path.read_text()).get("resources", [])
        for i in r.get("instances", [])
        if i.get("status") == "tainted"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform", required=True)
    parser.add_argument("--evidence")
    args = parser.parse_args()
    tf = args.terraform
    version = subprocess.run([tf, "version", "-json"], capture_output=True, check=True).stdout
    evidence: dict = {"terraform": json.loads(version)["terraform_version"], "steps": {}}
    work = Path(tempfile.mkdtemp(prefix="cmp-p53-"))
    env = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "PATH": "/usr/bin:/bin"}

    def run(cmd, timeout=120, grace=None):
        started = time.monotonic()
        result = run_graceful([tf, *cmd], work, env, timeout, grace_seconds=grace)
        return result, round(time.monotonic() - started, 1)

    try:
        (work / "main.tf").write_text(MAIN_TF)
        assert run(["init", "-input=false"])[0].returncode == 0

        applied, _ = run(["apply", "-auto-approve", "-input=false", "-no-color"])
        evidence["steps"]["1_apply"] = {"rc": applied.returncode, "state": state_names(work)}
        assert evidence["steps"]["1_apply"]["state"] == ["first", "slow"]

        planned, _ = run(["plan", "-destroy", "-input=false", "-out=destroy.tfplan"])
        shown, _ = run(["show", "-json", "destroy.tfplan"])
        review = destroy_addresses(json.loads(shown.stdout))
        destroyed, _ = run(["apply", "-input=false", "-no-color", "-lock=true", "destroy.tfplan"])
        evidence["steps"]["2_reviewed_destroy"] = {
            "plan_rc": planned.returncode,
            "review": review,
            "apply_rc": destroyed.returncode,
            "state_after": state_names(work),
        }
        assert review == ["terraform_data.first", "terraform_data.slow"]
        assert state_names(work) == []

        for step, grace in (("3_timeout_sigterm", 30), ("4_timeout_sigkill_only", 0)):
            for leftover in ("terraform.tfstate", "terraform.tfstate.backup", LOCK):
                (work / leftover).unlink(missing_ok=True)
            result, seconds = run(
                ["apply", "-auto-approve", "-input=false", "-no-color", "-var=slow_seconds=60"],
                timeout=4,
                grace=grace,
            )
            evidence["steps"][step] = {
                "returncode": result.returncode,
                "stopped": result.stopped,
                "seconds": seconds,
                "graceful_message": b"Gracefully shutting down" in result.stdout,
                "lock_left": (work / LOCK).exists(),
                "state": state_names(work),
                "tainted": tainted(work),
                "errored_tfstate": (work / "errored.tfstate").exists(),
            }
        s3, s4 = evidence["steps"]["3_timeout_sigterm"], evidence["steps"]["4_timeout_sigkill_only"]
        assert (s3["stopped"], s3["lock_left"], s3["state"]) == (
            "TERMINATED",
            False,
            ["first", "slow"],
        )
        assert (s4["stopped"], s4["lock_left"]) == ("KILLED", True)

        (work / LOCK).unlink(missing_ok=True)  # local equivalent of `terraform force-unlock`
        cleaned, _ = run(["destroy", "-auto-approve", "-input=false", "-no-color"])
        evidence["steps"]["5_cleanup"] = {"rc": cleaned.returncode, "state": state_names(work)}
        assert state_names(work) == []
    finally:
        shutil.rmtree(work, ignore_errors=True)
        evidence["workspace_removed"] = not work.exists()
    text = json.dumps(evidence, indent=2, ensure_ascii=False)
    print(text)
    if args.evidence:
        Path(args.evidence).write_text(text + "\n")


if __name__ == "__main__":
    main()
