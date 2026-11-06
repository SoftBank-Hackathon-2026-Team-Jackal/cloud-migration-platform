"""API runtime settings state for the demo server check (ticket 45).

    python -m cmp_api.runtime_report

Prints `name=state` lines only (`on`/`off`, profile names, `error:<CODE>`), never a key or value.
"""

from cmp_providers.env_file import EnvFileError

from cmp_api.config import settings
from cmp_api.env_store import configured_env_file_store
from cmp_api.start_request import StartRefused, load_target_profiles


def report() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        configured_env_file_store()
        result["env_store"] = "on"
    except EnvFileError as exc:
        result["env_store"] = f"error:{exc.code}"
    try:
        profiles = load_target_profiles(settings.planning_target_profiles_file)
        result["target_profiles"] = ",".join(sorted(profiles)) or "none"
    except StartRefused as exc:
        result["target_profiles"] = f"error:{exc.code}"
    link = settings.connection_platform_principal_arn and settings.connection_template_base_url
    result["connection_link"] = "on" if link else "off"
    result["platform_ecr_registry"] = "on" if settings.platform_ecr_registry else "off"
    return result


if __name__ == "__main__":
    for name, state in report().items():
        print(f"{name}={state}")
