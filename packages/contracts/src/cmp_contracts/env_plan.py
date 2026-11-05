"""Per-key `.env` handling plan and its rendered form (ENV-006, ENV-007; ticket ENV-03, #147).

Analysis emits an `EnvPlan` from key names only. Sandbox and Provisioning pass the same plan to
`cmp_providers.env_file.render_env`, which returns a `RenderedEnv`. Secret values live only in
`RenderedSecret.data` as `SecretStr`: `repr`, `model_dump` and JSON show `**********`, never the
value. Render inside the Activity that applies the result; never send a `RenderedEnv` across a
Temporal boundary.
"""

import json
from typing import Annotated, Literal

from pydantic import Field, SecretStr, model_validator

from cmp_contracts.analysis import EnvExceptions
from cmp_contracts.chart import KubernetesName, SecretKey
from cmp_contracts.provisioning import ContentRef, Contract
from cmp_contracts.shared_deployment_v2 import SecretRefV2

EnvKey = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
Disposition = Literal["PASS", "EXCLUDED", "REPLACED", "UNREPRESENTABLE", "MISSING"]
Binding = Literal[
    "host", "port", "name", "username", "password_secret_ref", "connection_url_secret_ref"
]


class EnvPlanEntry(Contract):
    key: EnvKey
    disposition: Disposition
    binding: Binding | None = None  # only for REPLACED
    reason: Code | None = None  # a code such as DSN_FORMAT, never prose or a value

    @model_validator(mode="after")
    def _binding_only_for_replaced(self) -> "EnvPlanEntry":
        if (self.disposition == "REPLACED") != (self.binding is not None):
            raise ValueError("BINDING_REQUIRED_EXACTLY_FOR_REPLACED")
        return self


class EnvPlan(Contract):
    env_file_ref: ContentRef
    entries: list[EnvPlanEntry]

    @model_validator(mode="after")
    def _unique_keys(self) -> "EnvPlan":
        keys = [e.key for e in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("DUPLICATE_ENV_KEY")
        return self


def to_env_exceptions(plan: EnvPlan, *, db_resource_id: str | None = None) -> EnvExceptions:
    """The web-facing `EnvExceptions` (`replaced` values are `<db resource id>.<binding>`).

    UNREPRESENTABLE and PASS entries appear in none of the three lists, as in
    `cmp_scanners.assembly.build_requirements`.
    """
    replaced = [e for e in plan.entries if e.disposition == "REPLACED"]
    if replaced and db_resource_id is None:
        raise ValueError("DB_RESOURCE_ID_REQUIRED")
    return EnvExceptions(
        excluded=sorted(e.key for e in plan.entries if e.disposition == "EXCLUDED"),
        replaced={e.key: f"{db_resource_id}.{e.binding}" for e in sorted(replaced, key=_key)},
        missing=sorted(e.key for e in plan.entries if e.disposition == "MISSING"),
    )


def _key(entry: EnvPlanEntry) -> str:
    return entry.key


class EnvDecision(Contract):
    """A key that blocks rendering; the caller fails with this structured code."""

    key: EnvKey
    code: Code


class EnvVarRef(Contract):
    """One container env entry: always a `secretKeyRef`, never a literal value."""

    name: EnvKey
    secret_ref: SecretRefV2


class RenderedSecret(Contract):
    """A Secret the applying actor creates. Values are `SecretStr`; read them with `reveal()`."""

    name: KubernetesName
    data: dict[SecretKey, SecretStr]

    def reveal(self) -> dict[str, str]:
        return {k: v.get_secret_value() for k, v in self.data.items()}


class RenderedEnv(Contract):
    env: list[EnvVarRef]
    secrets: list[RenderedSecret]
    decisions: list[EnvDecision]

    def canonical_bytes(self) -> bytes:
        """Stable JSON for digests and comparison; Secret values are masked."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
