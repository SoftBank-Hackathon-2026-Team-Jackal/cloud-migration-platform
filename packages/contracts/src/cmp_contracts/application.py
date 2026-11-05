"""Reference-only application Values; raw environment values never cross this boundary."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.chart import DynamicBinding, KubernetesName, SecretKey
from cmp_contracts.provisioning import Application, ContentRef, Contract, Image, Text

EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class ConfigBinding(Contract):
    kind: Literal["CONFIG_MAP"]
    name: KubernetesName
    key: SecretKey


class SecretBinding(Contract):
    kind: Literal["USER_SECRET"]
    name: KubernetesName
    key: SecretKey


class DatabaseBinding(Contract):
    kind: Literal["DATABASE"]
    field: Literal["host", "port", "name", "username", "secret_ref", "connection_url_secret_ref"]


class EnvironmentBinding(Contract):
    name: EnvName
    source: Annotated[ConfigBinding | SecretBinding | DatabaseBinding, Field(discriminator="kind")]


class DatabaseConnection(Contract):
    host: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")] | None
    port: int = Field(ge=1, le=65535)
    name: Text
    username: Text
    secret_name: KubernetesName | None
    secret_key: SecretKey
    # Dynamic slot for the platform URL Secret (ticket 19). Omitted while unset so Values
    # approved before this field existed keep their exact bytes.
    url_secret_name: KubernetesName | None = Field(default=None, exclude_if=lambda v: v is None)


class ApplicationValues(Contract):
    contract_version: Literal["1"]
    environment: Literal["SANDBOX", "PRODUCTION"]
    provider: Literal["AWS", "GCP", "ON_PREM"]
    release_name: Annotated[KubernetesName, Field(max_length=53)]
    namespace: KubernetesName
    config_ref: ContentRef
    image: Image
    application: Application
    database: DatabaseConnection
    required_env: list[EnvName]
    env: list[EnvironmentBinding]

    @model_validator(mode="after")
    def validate_bindings(self):
        names = [item.name for item in self.env]
        if len(names) != len(set(names)) or len(self.required_env) != len(set(self.required_env)):
            raise ValueError("Duplicate environment key")
        if set(names) != set(self.required_env):
            raise ValueError("Environment keys do not match declared metadata")
        if self.image.registry_auth_ref is not None:
            raise ValueError("Registry authentication resolution is not implemented")
        return self


class ApplicationPlan(Contract):
    values: ApplicationValues
    bindings: list[DynamicBinding]
