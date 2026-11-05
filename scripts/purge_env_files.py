"""Delete uploaded `.env` files past retention (ENV-01). Run on the API host, e.g. hourly by cron:

  uv run python scripts/purge_env_files.py

A file used by a migration is deleted 7 days after that Migration Workflow closed (Temporal
`close_time`); an unused upload 7 days after its last version. A Workflow Temporal no longer knows
closed before Temporal's own retention, so its file is deleted now. Prints purged IDs only, never values.
"""

import asyncio
import sys
from datetime import UTC, datetime

from cmp_api.config import settings
from cmp_api.temporal_client import get_temporal_client
from cmp_providers.env_file_store import EncryptedEnvFileStore, EnvFileError, load_key
from temporalio.service import RPCError, RPCStatusCode


async def closed_at(migration_ids: list[str]) -> dict[str, datetime | None]:
    client = await get_temporal_client()
    ended: dict[str, datetime | None] = {}
    for workflow_id in migration_ids:
        try:
            described = await client.get_workflow_handle(workflow_id).describe()
            ended[workflow_id] = described.close_time
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            ended[workflow_id] = datetime.min.replace(tzinfo=UTC)
    return ended


def main() -> int:
    try:
        if not settings.env_file_store_dir:
            raise EnvFileError("ENV_FILE_STORE_UNCONFIGURED")
        key = settings.env_file_encryption_key
        store = EncryptedEnvFileStore(
            settings.env_file_store_dir, load_key(key.get_secret_value() if key else None)
        )
    except EnvFileError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    ended = asyncio.run(closed_at(store.bound_migrations()))
    for file_id in store.purge_expired(ended.get):
        print(file_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
