"""Ticket 48 (#183) G1-G3: GCP platform/node service account script (fake gcloud on PATH)."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "infra/demo/gcp-platform-identity.sh"
PLATFORM = "serviceAccount:cmp-provisioning@sbh26-prod.iam.gserviceaccount.com"
NODES = "cmp-gke-nodes@sbh26-prod.iam.gserviceaccount.com"


def run(tmp_path, *args, project="sbh26-prod", existing=False):
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    gcloud = tools / "gcloud"
    gcloud.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        f'case "$*" in "config get-value project"*) printf "%s" "{project}";; '
        f'"iam service-accounts describe"*) exit {0 if existing else 1};; esac\n'
    )
    gcloud.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={"PATH": f"{tools}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return result, lines


def test_dry_run_only_reads_the_project_and_prints_the_plan(tmp_path):
    result, calls = run(tmp_path, "--configuration", "cmp-gcp-prod")
    assert result.returncode == 0, result.stderr
    assert calls == ["config get-value project --configuration cmp-gcp-prod"]
    assert "DRY RUN" in result.stdout
    for role in (
        "roles/container.admin",
        "roles/compute.networkAdmin",
        "roles/compute.securityAdmin",
        "roles/container.defaultNodeServiceAccount",
        "roles/iam.serviceAccountUser",
    ):
        assert role in result.stdout
    assert "roles/editor" not in result.stdout and "roles/owner" not in result.stdout
    # The key is never created by the script; only guidance is printed.
    assert "keys create" in result.stdout
    assert not [line for line in calls if "keys create" in line]


def test_apply_creates_accounts_and_bindings_but_never_a_key(tmp_path):
    result, calls = run(tmp_path, "--configuration", "cmp-gcp-prod", "--apply")
    assert result.returncode == 0, result.stderr
    assert sum(line.startswith("iam service-accounts create") for line in calls) == 2
    project_bindings = [
        line for line in calls if line.startswith("projects add-iam-policy-binding")
    ]
    assert len(project_bindings) == 4
    assert all(PLATFORM in line for line in project_bindings[:3])
    sa_binding = [line for line in calls if line.startswith("iam service-accounts add-iam")]
    assert sa_binding and NODES in sa_binding[0] and PLATFORM in sa_binding[0]
    assert not [line for line in calls if "keys create" in line]


def test_apply_keeps_existing_accounts(tmp_path):
    result, calls = run(tmp_path, "--configuration", "cmp-gcp-prod", "--apply", existing=True)
    assert result.returncode == 0, result.stderr
    assert not [line for line in calls if line.startswith("iam service-accounts create")]


def test_wrong_project_or_default_configuration_is_refused(tmp_path):
    result, calls = run(tmp_path, "--configuration", "cmp-gcp-prod", "--apply", project="other-1")
    assert result.returncode == 1
    assert calls == ["config get-value project --configuration cmp-gcp-prod"]
    result, _ = run(tmp_path, "--configuration", "default")
    assert result.returncode == 2
