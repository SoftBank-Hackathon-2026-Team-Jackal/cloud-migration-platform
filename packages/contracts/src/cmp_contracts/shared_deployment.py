"""Shared Sandbox/production workload rendering request and result."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.application import ApplicationValues, ConfigBinding, EnvName, SecretBinding
from cmp_contracts.chart import KubernetesName, PostgresChartValues, SecretKey
from cmp_contracts.provisioning import Application, ContentRef, Contract, Image, Positive, Text


class ReleaseNames(Contract):
    app: Annotated[KubernetesName, Field(max_length=53)]
    db: Annotated[KubernetesName, Field(max_length=53)]

    @model_validator(mode="after")
    def distinct(self):
        if self.app == self.db:
            raise ValueError("Application and database releases must differ")
        return self


class SharedApplication(Contract):
    image: Image
    config_ref: ContentRef
    replicas: Positive
    cpu_millicores: Positive
    memory_mib: Positive
    port: int = Field(ge=1, le=65535)
    health_path: Annotated[str, Field(pattern=r"^/\S*$")]

    def application(self) -> Application:
        return Application(
            replicas=self.replicas,
            cpu_millicores=self.cpu_millicores,
            memory_mib=self.memory_mib,
            port=self.port,
            health_path=self.health_path,
        )


class SharedDatabase(Contract):
    image: Image
    version: Literal["16"]
    name: Text
    username: Text
    password_secret_name: KubernetesName
    password_secret_key: SecretKey
    cpu_millicores: Positive
    memory_mib: Positive
    pvc_size_gib: Positive


class SharedDeploymentRequest(Contract):
    contract_version: Literal["1"]
    environment: Literal["SANDBOX", "PRODUCTION"]
    provider: Literal["AWS", "GCP", "ON_PREM"]
    namespace: KubernetesName
    release_names: ReleaseNames
    storage_class: KubernetesName
    app: SharedApplication
    db: SharedDatabase
    env_bindings: dict[EnvName, str | ConfigBinding | SecretBinding]

    @model_validator(mode="after")
    def references_only(self):
        if (
            self.app.image.registry_auth_ref is not None
            or self.db.image.registry_auth_ref is not None
        ):
            raise ValueError("Registry authentication resolution is not implemented")
        allowed = {
            "database.host",
            "database.port",
            "database.name",
            "database.username",
            "database.secret_ref",
        }
        if any(
            isinstance(source, str) and source not in allowed
            for source in self.env_bindings.values()
        ):
            raise ValueError("Invalid database binding")
        return self


class DatabaseService(Contract):
    host: Text
    port: Literal[5432]


class SecretReference(Contract):
    secret_name: KubernetesName
    secret_key: SecretKey


class RenderValidation(Contract):
    passed: Literal[True] = True
    errors: list[str] = Field(default_factory=list)


class RenderedDatabase(Contract):
    release_name: KubernetesName
    helm_values: PostgresChartValues
    manifest_yaml: str
    service: DatabaseService


class RenderedApplication(Contract):
    release_name: KubernetesName
    helm_values: ApplicationValues
    manifest_yaml: str


class SharedDeploymentResult(Contract):
    environment: Literal["SANDBOX", "PRODUCTION"]
    provider: Literal["AWS", "GCP", "ON_PREM"]
    namespace: KubernetesName
    database: RenderedDatabase
    application: RenderedApplication
    bindings: dict[EnvName, str | SecretReference]
    validation: RenderValidation = Field(default_factory=RenderValidation)
