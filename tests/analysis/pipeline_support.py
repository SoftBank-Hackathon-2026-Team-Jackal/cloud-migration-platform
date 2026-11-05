"""Shared fixtures for the analysis pipeline and integration tests."""

import shutil
import subprocess
from pathlib import Path

from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.analysis import AnalysisInput
from cmp_worker.analysis_pipeline import AnalysisPipeline
from cmp_worker.artifact_store import LocalArtifactStore

ROOT = Path(__file__).parent.parent.parent
SECRET = "not-a-real-secret-value"
AWS_KEY = "AKIAABCDEFGHIJKLMNOP"
MAIN_GO = """package main

import (
\t"net/http"
\t"os"

\t"github.com/jackc/pgx/v5"
)

func main() {
\t_ = os.Getenv("DB_HOST")
\t_ = os.Getenv("DB_PORT")
\t_ = os.Getenv("DB_NAME")
\t_ = os.Getenv("DB_USER")
\t_ = os.Getenv("DB_PASSWORD")
\t_ = os.Getenv("APP_PORT")
\t_ = os.Getenv("FEATURE_X")
\t_, _ = http.Get("https://api.pay.example.com/v1")
\t_ = pgx.ErrNoRows
}
"""


def git(cwd, *args):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }  # fmt: skip
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def build_repo(tmp_path):
    work = tmp_path / "work"
    (work / "k8s").mkdir(parents=True)
    (work / "main.go").write_text(MAIN_GO)
    (work / "Dockerfile").write_text("FROM scratch\nCOPY main.go .\n")
    (work / ".env").write_text(f"DB_PASSWORD={SECRET}\nAPP_PORT=8080\n")
    shutil.copy(ROOT / "tests/fixtures/analysis/source-like/stack.yaml", work / "k8s/stack.yaml")
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-f", ".")
    git(work, "commit", "-q", "-m", "v1")
    sha = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return {"url": str(bare), "sha": sha, "work": work, "tmp": tmp_path}


def make(repo, name="a", **kw):
    base = repo["tmp"] / name
    kw.setdefault("llm_factory", None)
    kw.setdefault("aws_clients_factory", None)
    return AnalysisPipeline(
        store=LocalArtifactStore(base / "store"),
        workspace_root=base / "ws",
        skills_root=ROOT / "skills",
        catalog_dir=ROOT / "catalog",
        allow_local_repositories=True,
        **kw,
    )


def analysis_input(repo, **kw):
    return AnalysisInput(
        migration_id="mig-1",
        repository=repo["url"],
        requested_commit=repo["sha"],
        manifests_dir="k8s",
        **kw,
    ).model_dump(mode="json")


def run_to_requirements(pipeline, inp):
    source = pipeline.fetch_source(inp)
    code = pipeline.scan_code(inp, source)
    manifests = pipeline.scan_manifests(inp, source)
    infra = pipeline.scan_aws_infra(inp)
    built = pipeline.build_requirements(inp, source, code, manifests, infra)
    return source, code, manifests, infra, built


def row(capability, usage, locators, **kw):
    fields = {
        "id": "tmp-" + capability, "capability": capability, "usage": usage, "grade": "DIRECT",
        "required_proposed": True, "required_reason": "core", "disposition": "MIGRATE",
        "loses": [], "evidence": [{"kind": "CODE", "locator": loc} for loc in locators],
    }  # fmt: skip
    fields.update(kw)
    return fields


def mapping_llm(*rows):
    return lambda *_: FakeLLMClient(
        [LLMResponse(tool_calls=[call("submit_mapping", rows=list(rows))])]
    )
