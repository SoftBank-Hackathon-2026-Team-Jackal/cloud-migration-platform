"""Platform egress inside the cluster API access CIDRs (ticket 47, D-10·B-21).

After Apply the worker reaches the new cluster's public API endpoint for kubectl/Helm. If the
reviewed `api_access_cidrs` leave out the platform's egress address, infrastructure is created and
the workloads then time out. Planning refuses that input before any cloud call. Provider-neutral
so the GKE Plan (`master_authorized_networks`) can call the same check.
"""

from collections.abc import Sequence
from ipaddress import IPv4Network, ip_network

from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue


def _fail(code: str, path: str, message: str):
    raise ProvisioningInputError([ValidationIssue(code=code, path=path, message=message)])


def check_platform_egress(
    api_access_cidrs: Sequence[str], platform_egress_cidrs: Sequence[str], *, path: str
) -> None:
    """Every configured egress CIDR must lie inside one of the (already validated) API CIDRs."""
    allowed = [ip_network(cidr, strict=True) for cidr in api_access_cidrs]
    for value in platform_egress_cidrs:
        try:
            egress = ip_network(value, strict=True)
        except ValueError:
            egress = None
        if not isinstance(egress, IPv4Network) or "/" not in value:
            _fail(
                "INVALID_PLATFORM_EGRESS",
                "platform_egress_cidrs",
                "플랫폼 egress 설정은 정규화된 IPv4 CIDR이어야 합니다.",
            )
        if not any(egress.subnet_of(network) for network in allowed):
            _fail(
                "PLATFORM_EGRESS_NOT_ALLOWED",
                path,
                "API 접근 CIDR에 플랫폼 egress 주소가 없어 구축 뒤 kubectl·Helm이 닿지 못합니다.",
            )
