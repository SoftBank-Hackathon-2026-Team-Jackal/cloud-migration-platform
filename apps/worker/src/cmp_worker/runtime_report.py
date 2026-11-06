"""Which real paths this worker would register, for the demo server check (ticket 45).

    python -m cmp_worker.runtime_report

Prints `name=state` lines only: `real`/`scaffold`, `on`/`off` or `error:<CODE>`. Never a setting
value, path or key. It calls the same builders the workers use at start-up, so the answer cannot
drift from what `run_all` registers.
"""

import re

from cmp_worker.config import Settings

_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _error(exc: Exception) -> str:
    code = getattr(exc, "code", None) or (exc.args[0] if exc.args else None)
    return f"error:{code}" if isinstance(code, str) and _CODE.match(code) else "error:UNKNOWN"


def report(config: Settings) -> dict[str, str]:
    from cmp_api.env_store import optional_env_file_store

    from cmp_worker.build_wiring import BuildSettings
    from cmp_worker.llm_factory import build_llm_factory
    from cmp_worker.workers.analysis import build_pipeline
    from cmp_worker.workers.sandbox import build_sandbox_loop

    result: dict[str, str] = {}
    try:
        result["analysis"] = "real" if build_pipeline(config) is not None else "scaffold"
    except Exception as exc:  # noqa: BLE001 - reported as a code, the check goes on
        result["analysis"] = _error(exc)
    try:
        result["sandbox"] = "real" if build_sandbox_loop(config) is not None else "scaffold"
    except Exception as exc:  # noqa: BLE001
        result["sandbox"] = _error(exc)
    try:
        build = BuildSettings.from_values(
            config.build_source_bucket,
            config.build_ecr_repository,
            config.build_log_bucket,
            config.build_platform,
            config.build_aws_profile,
        )
        result["build"] = "on" if build is not None else "off"
    except ValueError as exc:
        result["build"] = _error(exc)
    result["llm"] = "on" if build_llm_factory(config) is not None else "off"
    result["env_store"] = "on" if optional_env_file_store() is not None else "off"
    try:  # ON_PREM rollout jobs are built in the worker from the API's registry settings
        from cmp_api.onprem_rollout import configured_platform_registry

        result["platform_ecr_registry"] = "on" if configured_platform_registry() else "off"
    except ValueError as exc:
        result["platform_ecr_registry"] = _error(exc)
    return result


if __name__ == "__main__":
    from cmp_worker.config import settings

    for name, state in report(settings).items():
        print(f"{name}={state}")
