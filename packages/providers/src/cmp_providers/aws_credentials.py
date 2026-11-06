"""Which named AWS profile runs each part of an AWS Plan/Apply/workload run (ticket 46, ADR-005).

`credential_ref` (the Plan input's existing `VersionRef`) names the target environment
connection when its `id` is that connection's profile name `cmp-conn-<12 hex>` (ADR-005; what the
start request writes, ticket 44) or its connection ID `conn-<12 hex>`. The generated assume-role
profile `cmp-conn-<12 hex>` (ticket 23) then runs the Terraform provider, `aws sts`,
`aws eks` and kubectl. The Terraform State backend always stays on the platform profile: the
State bucket is in the platform account (B-13).

Any other `id` keeps the pre-46 behavior: the worker's fixed platform profile runs everything
(demo same-account target). Pure; reads no file and makes no call.
"""

import re
from dataclasses import dataclass

from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue, VersionRef

CONNECTION_PREFIX = "conn-"
PROFILE_PREFIX = "cmp-conn-"
# Connection IDs are `conn-` + 12 hex today (ticket 23); up to 32 is accepted, nothing else.
_CONNECTION_ID = re.compile(r"conn-([0-9a-f]{12,32})")


@dataclass(frozen=True)
class AwsProfiles:
    provider: str  # target account: Terraform provider, sts identity, eks, kubectl
    state: str  # platform account: Terraform S3 backend
    connection_id: str | None  # None: fixed platform profile (no connection)


def _reject(code: str, path: str, message: str):
    raise ProvisioningInputError([ValidationIssue(code=code, path=path, message=message)])


def connection_profile(connection_id: str) -> str:
    match = _CONNECTION_ID.fullmatch(connection_id)
    if match is None:
        _reject("INVALID_CREDENTIAL_REF", "credential_ref.id", "연결 ID 형식이 아닙니다.")
    return PROFILE_PREFIX + match.group(1)


def connection_id_of(credential_ref: VersionRef) -> str | None:
    """The connection ID a `credential_ref` names, or None for the fixed platform profile.

    Both `cmp-conn-<hex>` and `conn-<hex>` name a connection; anything else that starts with
    either prefix is malformed (`INVALID_CREDENTIAL_REF`), never a silent fallback.
    """
    value = credential_ref.id
    if value.startswith(PROFILE_PREFIX):
        value = value.removeprefix("cmp-")
    elif not value.startswith(CONNECTION_PREFIX):
        return None
    connection_profile(value)  # validates the format
    return value


def resolve_aws_profiles(
    credential_ref: VersionRef, *, platform_profile: str | None
) -> AwsProfiles:
    if not platform_profile or platform_profile == "default":
        _reject("MISSING_PROFILE", "aws_profile", "이름이 지정된 AWS 프로필이 필요합니다.")
    connection_id = connection_id_of(credential_ref)
    if connection_id is None:
        return AwsProfiles(provider=platform_profile, state=platform_profile, connection_id=None)
    return AwsProfiles(
        provider=connection_profile(connection_id),
        state=platform_profile,
        connection_id=connection_id,
    )
