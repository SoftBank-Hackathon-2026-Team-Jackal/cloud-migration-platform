"""Pre-flight checks for running the real first cycle (ticket 22).

Nothing here spends money or changes anything: it looks at tools, directories, the dedicated
cluster and the settings, and says what is missing. The model key is only checked for presence
and is never printed.
"""

import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cmp_worker.build_wiring import BuildSettings
from cmp_worker.config import Settings
from cmp_worker.prebuilt_builder import BuildUnavailable, PrebuiltImages
from cmp_worker.sandbox_deploy import SANDBOX_CONTEXT

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""


def _run(command: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout + done.stderr


def _connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".cmp-preflight"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def run_checks(
    config: Settings,
    *,
    api_store_dir: str | None = None,
    repository: str | None = None,
    run: Callable[[list[str]], tuple[int, str]] = _run,
    which: Callable[[str], str | None] = shutil.which,
    connect: Callable[[str, int], bool] = _connect,
    environ: dict[str, str] | None = None,
) -> list[Check]:
    env = os.environ if environ is None else environ
    out: list[Check] = []

    for tool, needed in (("git", True), ("helm", True), ("kubectl", True), ("docker", False)):
        if which(tool) is None:
            out.append(Check(f"tool:{tool}", FAIL if needed else WARN, "not found on PATH"))
        else:
            out.append(Check(f"tool:{tool}", OK))
    if which("helm"):
        code, text = run(["helm", "version", "--short"])
        major = text.strip().lstrip("v").split(".")[0]
        out.append(
            Check("helm:version", OK if major == "3" else FAIL, text.strip() or "unknown")
            if code == 0
            else Check("helm:version", FAIL, "could not run helm")
        )

    host, _, port = config.temporal_address.partition(":")
    reachable = connect(host or "localhost", int(port or 7233))
    out.append(Check("temporal", OK if reachable else FAIL, config.temporal_address))

    store = config.analysis_artifact_store_dir
    workspace = config.analysis_workspace_root
    out.append(
        Check("analysis:store", OK, store)
        if store and _writable(Path(store))
        else Check("analysis:store", FAIL, "ANALYSIS_ARTIFACT_STORE_DIR unset or not writable")
    )
    out.append(
        Check("analysis:workspace", OK, workspace)
        if workspace and _writable(Path(workspace))
        else Check("analysis:workspace", FAIL, "ANALYSIS_WORKSPACE_ROOT unset or not writable")
    )
    if api_store_dir and store and Path(api_store_dir).resolve() != Path(store).resolve():
        out.append(
            Check(
                "store:shared-with-planning",
                WARN,
                "ARTIFACT_STORE_DIR differs from the analysis store, so Planning cannot read the "
                "requirements the analysis writes",
            )
        )
    elif api_store_dir and store:
        out.append(Check("store:shared-with-planning", OK))

    kubeconfig = config.sandbox_kubeconfig
    if not kubeconfig or not Path(kubeconfig).expanduser().is_file():
        out.append(Check("sandbox:kubeconfig", FAIL, "SANDBOX_KUBECONFIG unset or not a file"))
    else:
        code, text = run(
            [
                "kubectl",
                "--kubeconfig",
                str(Path(kubeconfig).expanduser()),
                "--context",
                SANDBOX_CONTEXT,
                "get",
                "namespace",
                "-o",
                "name",
            ]
        )
        out.append(
            Check("sandbox:cluster", OK, SANDBOX_CONTEXT)
            if code == 0
            else Check("sandbox:cluster", FAIL, f"cannot reach context {SANDBOX_CONTEXT}")
        )
    try:
        images = PrebuiltImages.from_json(config.sandbox_prebuilt_images)
        configured = bool(config.sandbox_prebuilt_images)
        out.append(
            Check(
                "sandbox:images",
                OK if configured else WARN,
                "digest-pinned"
                if configured
                else "SANDBOX_PREBUILT_IMAGES unset: no image for the loop",
            )
        )
        del images
    except BuildUnavailable as exc:
        out.append(Check("sandbox:images", FAIL, exc.code))

    try:
        build = BuildSettings.from_values(
            config.build_source_bucket,
            config.build_ecr_repository,
            config.build_log_bucket,
            config.build_platform,
            config.build_aws_profile,
        )
        out.append(
            Check("build", OK, "real build path (source to S3, Build module)")
            if build
            else Check("build", WARN, "BUILD_* unset: pre-built images only, no build evidence")
        )
    except ValueError:
        out.append(Check("build", FAIL, "BUILD_SETTINGS_INCOMPLETE"))

    if not config.llm_enabled:
        out.append(Check("llm", WARN, "LLM_ENABLED is off: mapping and fixing will not run"))
    else:
        problems = []
        if not env.get("ANTHROPIC_API_KEY"):
            problems.append("ANTHROPIC_API_KEY not set")
        if not config.llm_ledger_path:
            problems.append("LLM_LEDGER_PATH not set")
        elif not _writable(Path(config.llm_ledger_path).parent):
            problems.append("ledger directory not writable")
        if config.llm_kill_switch_path and Path(config.llm_kill_switch_path).exists():
            problems.append("kill switch file exists")
        if env.get("CMP_LLM_DISABLED", "").lower() in {"1", "true", "yes"}:
            problems.append("CMP_LLM_DISABLED is set")
        out.append(
            Check("llm", FAIL, "; ".join(problems))
            if problems
            else Check(
                "llm",
                OK,
                f"{config.llm_model}, limits ${config.llm_limit_run_usd:g}/run "
                f"${config.llm_limit_day_usd:g}/day ${config.llm_limit_month_usd:g}/month",
            )
        )

    if config.source_token_resolver == "env":
        has = bool(env.get("CMP_GITHUB_TOKEN"))
        out.append(
            Check("source:token", OK, "CMP_GITHUB_TOKEN is set")
            if has
            else Check(
                "source:token", FAIL, "SOURCE_TOKEN_RESOLVER=env but CMP_GITHUB_TOKEN is not set"
            )
        )
    elif config.source_token_resolver == "none":
        out.append(Check("source:token", OK, "public repositories only"))
    else:
        out.append(Check("source:token", FAIL, "SOURCE_TOKEN_RESOLVER must be none or env"))

    if repository:
        path = Path(repository).expanduser()
        if path.exists():
            code, _ = run(["git", "-C", str(path), "rev-parse", "--git-dir"])
            out.append(Check("source:repository", OK if code == 0 else FAIL, str(path)))
            if code == 0 and not config.analysis_allow_local_repositories:
                out.append(
                    Check(
                        "source:local-clone",
                        FAIL,
                        "set ANALYSIS_ALLOW_LOCAL_REPOSITORIES=true (development only) to read it",
                    )
                )
        else:
            out.append(Check("source:repository", WARN, "not a local path; it will be cloned"))
    return out


def summary(checks: list[Check]) -> str:
    return json.dumps({c.name: [c.status, c.detail] for c in checks}, ensure_ascii=False, indent=2)


def ok(checks: list[Check]) -> bool:
    return not any(c.status == FAIL for c in checks)
