"""Resource-ID based Sandbox and Provisioning render contracts; no cluster operations."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.application import EnvName
from cmp_contracts.chart import KubernetesName, PodResources, PostgresChartValues, SecretKey
from cmp_contracts.provisioning import Application, ContentRef, Contract, Image, Positive, Text
from cmp_contracts.provisioning_v2 import ResourceId, ResourceOutputBinding
from cmp_contracts.shared_deployment import RenderValidation, SecretReference

ReleaseName = Annotated[KubernetesName, Field(max_length=53)]
Environment = Literal["SANDBOX", "PRODUCTION"]
Provider = Literal["AWS", "GCP", "ON_PREM"]


class SecretRefV2(Contract):
    name: KubernetesName
    key: SecretKey


class SecretInputBindingV2(Contract):
    secret_ref: SecretRefV2


class DatabaseIdentityV2(Contract):
    name: Text
    username: Text
    password_secret_ref: SecretRefV2


class StorageV2(Contract):
    class_name: KubernetesName
    size_gib: Positive


class DatabaseResourceV2(Contract):
    id: ResourceId
    kind: Literal["POSTGRESQL"]
    release_name: ReleaseName
    image: Image
    version: Literal["16"]
    extensions: list[Text] = Field(default_factory=list)
    database: DatabaseIdentityV2
    resources: PodResources
    storage: StorageV2


class ApplicationResourceV2(Contract):
    id: ResourceId
    kind: Literal["APPLICATION"]
    release_name: ReleaseName
    image: Image
    config_ref: ContentRef | None = None
    replicas: Positive
    resources: PodResources
    port: int = Field(ge=1, le=65535)
    health_path: Annotated[str, Field(pattern=r"^/\S*$")]
    env_bindings: dict[EnvName, ResourceOutputBinding | SecretInputBindingV2] = Field(
        default_factory=dict
    )


DeploymentResourceV2 = Annotated[
    ApplicationResourceV2 | DatabaseResourceV2, Field(discriminator="kind")
]


class SharedDeploymentRequestV2(Contract):
    contract_version: Literal["2"]
    environment: Environment
    provider: Provider
    namespace: KubernetesName
    resources: list[DeploymentResourceV2] = Field(min_length=1)

    @model_validator(mode="after")
    def no_registry_auth_ref(self):
        if any(resource.image.registry_auth_ref is not None for resource in self.resources):
            raise ValueError("Registry authentication resolution is not implemented")
        return self


class LiteralEnvSourceV2(Contract):
    kind: Literal["LITERAL"]
    value: Text


class SecretEnvSourceV2(Contract):
    kind: Literal["SECRET"]
    name: KubernetesName
    key: SecretKey


class ResolvedEnvV2(Contract):
    name: EnvName
    source: Annotated[LiteralEnvSourceV2 | SecretEnvSourceV2, Field(discriminator="kind")]


class ApplicationValuesV2(Contract):
    contract_version: Literal["2"]
    environment: Environment
    provider: Provider
    release_name: ReleaseName
    namespace: KubernetesName
    image: Image
    config_ref: ContentRef | None = None
    application: Application
    required_env: list[EnvName]
    env: list[ResolvedEnvV2]

    @model_validator(mode="after")
    def complete_environment(self):
        actual = [item.name for item in self.env]
        if len(actual) != len(set(actual)) or len(self.required_env) != len(set(self.required_env)):
            raise ValueError("Duplicate environment key")
        if set(actual) != set(self.required_env):
            raise ValueError("Environment keys do not match")
        if self.image.registry_auth_ref is not None:
            raise ValueError("Registry authentication resolution is not implemented")
        return self


class DatabaseOutputsV2(Contract):
    host: Text
    port: Literal[5432]
    name: Text
    username: Text
    password_secret_ref: SecretRefV2
    # Secret the deploying actor creates with the connection URL (ticket 19, U2/U4).
    connection_url_secret_ref: SecretRefV2


class RenderedDatabaseV2(Contract):
    id: ResourceId
    kind: Literal["POSTGRESQL"] = "POSTGRESQL"
    release_name: ReleaseName
    helm_values: PostgresChartValues
    manifest_yaml: str
    outputs: DatabaseOutputsV2


class RenderedApplicationV2(Contract):
    id: ResourceId
    kind: Literal["APPLICATION"] = "APPLICATION"
    release_name: ReleaseName
    helm_values: ApplicationValuesV2
    manifest_yaml: str
    resolved_env_bindings: dict[EnvName, str | SecretReference]


RenderedResourceV2 = Annotated[
    RenderedApplicationV2 | RenderedDatabaseV2, Field(discriminator="kind")
]


class SharedDeploymentResultV2(Contract):
    environment: Environment
    provider: Provider
    namespace: KubernetesName
    resources: list[RenderedResourceV2]
    validation: RenderValidation = Field(default_factory=RenderValidation)
