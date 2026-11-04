"""PostgreSQL Chart input shared by Sandbox and Production renderers."""

from typing import Annotated, Literal

from pydantic import Field

from cmp_contracts.provisioning import Contract, Image, Positive, Text

KubernetesName = Annotated[
    str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
]
SecretKey = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9._-]+$")]


class DatabaseSettings(Contract):
    version: Literal["16"]
    name: Text
    username: Text
    secret_name: KubernetesName
    secret_key: SecretKey


class PodResources(Contract):
    cpu_millicores: Positive
    memory_mib: Positive


class PersistentStorage(Contract):
    class_name: KubernetesName
    size_gib: Positive


class PostgresChartValues(Contract):
    contract_version: Literal["1"]
    environment: Literal["SANDBOX", "PRODUCTION"]
    provider: Literal["AWS", "GCP", "ON_PREM"]
    release_name: Annotated[KubernetesName, Field(max_length=53)]
    namespace: KubernetesName
    image: Image
    database: DatabaseSettings
    resources: PodResources
    storage: PersistentStorage


class DynamicBinding(Contract):
    """Only these database connection slots may change after approval."""

    target: Literal["database.host", "database.secret_name", "database.url_secret_name"]
    source: Literal["DATABASE_HOST", "DATABASE_SECRET_NAME", "DATABASE_URL_SECRET_NAME"]
