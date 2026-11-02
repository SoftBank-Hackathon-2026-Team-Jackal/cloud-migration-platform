from functools import lru_cache

from temporalio.client import Client

from cmp_api.config import settings


@lru_cache(maxsize=1)
def _connection_settings() -> tuple[str, str]:
    return settings.temporal_address, settings.temporal_namespace


async def get_temporal_client() -> Client:
    address, namespace = _connection_settings()
    return await Client.connect(address, namespace=namespace)
