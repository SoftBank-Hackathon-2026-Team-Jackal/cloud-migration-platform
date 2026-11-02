"""References to immutable, access-controlled artifacts; no raw storage URLs."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cmp_contracts.provisioning import Contract, VersionRef

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ArtifactKind = Literal["SPEC", "TERRAFORM_PLAN", "CHART", "VALUES", "BINDINGS", "MANIFEST"]


class ArtifactRef(VersionRef):
    kind: ArtifactKind
    sha256: Digest
    size_bytes: int = Field(ge=0)


class RenderedChart(Contract):
    """Stored render outputs. A valid reference is not proof of a live deployment."""

    contract_version: Literal["1"]
    chart: ArtifactRef
    values: ArtifactRef
    manifest: ArtifactRef

    @model_validator(mode="after")
    def check_roles(self):
        if (self.chart.kind, self.values.kind, self.manifest.kind) != (
            "CHART",
            "VALUES",
            "MANIFEST",
        ):
            raise ValueError("Rendered chart artifact roles do not match")
        return self
