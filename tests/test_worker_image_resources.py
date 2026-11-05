"""Ticket 43 (#178): the worker image finds Terraform templates, Helm charts and analysis resources.

The installed packages live in site-packages, so the repository root is configured explicitly
(`CMP_RESOURCE_ROOT`) instead of being derived from `__file__`. The real image check (paths,
`terraform validate`, `helm template`) is recorded in the ticket.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from cmp_providers.resources import RUNTIME_RESOURCE_DIRS, resource_root

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "infra" / "demo" / "Dockerfile"
PLATFORMS = ("linux_amd64", "linux_arm64", "darwin_arm64", "darwin_amd64")


def test_resource_root_defaults_to_the_source_tree(monkeypatch):
    monkeypatch.delenv("CMP_RESOURCE_ROOT", raising=False)
    assert resource_root() == ROOT


def test_resource_root_uses_the_configured_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("CMP_RESOURCE_ROOT", str(tmp_path))
    assert resource_root() == tmp_path


def test_relative_resource_root_is_rejected(monkeypatch):
    monkeypatch.setenv("CMP_RESOURCE_ROOT", "infra")
    with pytest.raises(ValueError, match="CMP_RESOURCE_ROOT"):
        resource_root()


def test_every_resource_path_follows_the_configured_root(tmp_path):
    probe = """
import json
from cmp_providers import aws_apply, gcp_apply
from cmp_worker import plan_publish
from cmp_worker.workers import analysis
print(json.dumps({
    "aws": str(aws_apply.TEMPLATE_ROOT),
    "gcp": str(gcp_apply.GCP_TEMPLATE_ROOT),
    "db_chart": str(plan_publish.DB_CHART),
    "app_chart": str(plan_publish.APP_CHART),
    "analysis": str(analysis._REPO_ROOT),
}))
"""
    env = {**os.environ, "CMP_RESOURCE_ROOT": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
    )
    paths = json.loads(out.stdout)
    assert paths == {
        "aws": str(tmp_path / "infra/terraform/aws"),
        "gcp": str(tmp_path / "infra/terraform/gcp"),
        "db_chart": str(tmp_path / "infra/helm/postgres"),
        "app_chart": str(tmp_path / "infra/helm/application"),
        "analysis": str(tmp_path),
    }


def test_no_module_derives_the_repository_root_from_its_own_location():
    sources = [*ROOT.glob("apps/*/src/**/*.py"), *ROOT.glob("packages/*/src/**/*.py")]
    offenders = [
        str(path.relative_to(ROOT))
        for path in sources
        if re.search(r"__file__\)\.resolve\(\)\.parents\[", path.read_text())
        and path.name != "resources.py"
    ]
    assert offenders == []


def test_runtime_resources_exist_in_the_repository():
    for relative in RUNTIME_RESOURCE_DIRS:
        assert (ROOT / relative).is_dir(), relative


def _copied_dirs() -> set[str]:
    text = DOCKERFILE.read_text()
    return {
        dest.removeprefix("./")
        for src, dest in re.findall(r"^COPY (\S+) (\S+)$", text, re.MULTILINE)
        if src.removeprefix("./") == dest.removeprefix("./")
    }


def test_image_copies_every_runtime_resource_at_the_same_relative_path():
    copied = _copied_dirs()
    missing = [
        relative
        for relative in RUNTIME_RESOURCE_DIRS
        if not any(relative == c or relative.startswith(c + "/") for c in copied)
    ]
    assert missing == []


def test_image_points_the_resource_root_at_the_workdir():
    text = DOCKERFILE.read_text()
    workdir = re.search(r"^WORKDIR (\S+)$", text, re.MULTILINE).group(1)
    assert re.search(rf"^ENV .*CMP_RESOURCE_ROOT={re.escape(workdir)}\b", text, re.MULTILINE)


@pytest.mark.parametrize("cloud", ["aws", "gcp"])
def test_provider_lock_covers_linux_and_macos(cloud):
    lock = (ROOT / "infra" / "terraform" / cloud / ".terraform.lock.hcl").read_text()
    blocks = re.findall(r'provider "([^"]+)" \{(.*?)\n\}', lock, re.DOTALL)
    assert blocks
    for name, body in blocks:
        h1 = re.findall(r'"h1:[^"]+"', body)
        # `providers lock` records one h1 per platform package (identical packages may collapse).
        assert len(set(h1)) >= len(PLATFORMS), (name, len(h1))
