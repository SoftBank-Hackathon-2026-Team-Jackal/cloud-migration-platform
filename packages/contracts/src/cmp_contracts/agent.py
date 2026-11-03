"""Sandbox-only Agent work envelope; production approval is a separate boundary."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.artifact import ArtifactRef
from cmp_contracts.chart import KubernetesName
from cmp_contracts.provisioning import Contract, Text


class SandboxJob(Contract):
    mode: Literal["SANDBOX_TEST"]
    job_id: KubernetesName
    agent_id: KubernetesName
    cluster_uid: Text
    namespace: Annotated[str, Field(pattern=r"^cmp-test-[a-z0-9-]+$", max_length=63)]
    namespace_uid: Text
    storage_class: KubernetesName
    database_release: KubernetesName
    application_release: KubernetesName
    database_manifest: ArtifactRef
    application_manifest: ArtifactRef
    health_path: Annotated[str, Field(pattern=r"^/[a-zA-Z0-9/_-]*$")]
    application_port: int = Field(ge=1, le=65535)

    @model_validator(mode="after")
    def manifest_kinds(self):
        if any(
            ref.kind != "MANIFEST" for ref in [self.database_manifest, self.application_manifest]
        ):
            raise ValueError("Manifest artifact required")
        return self
