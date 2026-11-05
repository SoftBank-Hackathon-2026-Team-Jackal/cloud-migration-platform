"""Platform `.env` store wiring from settings (ENV-01 store; ENV-04 readers).

Shared by the upload API, the Agent env-secrets API and the Provisioning workers.
"""

from cmp_providers.env_file import EnvFileError
from cmp_providers.env_file_store import EncryptedEnvFileStore, load_key

from cmp_api.config import settings


def configured_env_file_store() -> EncryptedEnvFileStore:
    """Raises `EnvFileError` (`ENV_FILE_STORE_UNCONFIGURED`, `ENV_FILE_KEY_INVALID`)."""
    if not settings.env_file_store_dir:
        raise EnvFileError("ENV_FILE_STORE_UNCONFIGURED")
    key = settings.env_file_encryption_key
    return EncryptedEnvFileStore(
        settings.env_file_store_dir, load_key(key.get_secret_value() if key else None)
    )


def optional_env_file_store() -> EncryptedEnvFileStore | None:
    """Worker wiring: None when unconfigured; an EnvPlan then fails with a code at use."""
    try:
        return configured_env_file_store()
    except EnvFileError:
        return None
