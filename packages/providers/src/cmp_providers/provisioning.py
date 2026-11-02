"""Pure conversion of validated requirements; no credentials, cloud calls or Apply."""

from ipaddress import IPv4Network, ip_network

from cmp_contracts.provisioning import (
    ProviderConfiguration,
    ProvisioningInputError,
    ProvisioningSpec,
    ValidationIssue,
)


def validate_target(spec: ProvisioningSpec) -> list[ValidationIssue]:
    """Check supplied metadata only. Live CIDRs, permissions and instance catalog are not queried."""
    issues = []

    def reject(code, path, message):
        issues.append(ValidationIssue(code=code, path=f"target.{path}", message=message))

    target = spec.target
    architecture = (
        target.architecture if target.provider == "ON_PREM" else target.nodes.architecture
    )
    if architecture not in spec.image.platforms:
        reject(
            "ARCHITECTURE_MISMATCH",
            "architecture",
            "Image does not support the requested Node platform.",
        )
    if target.provider == "ON_PREM":
        return issues
    network = target.network
    if network.mode == "EXISTING" and network.network_ref is None:
        reject(
            "MISSING_NETWORK_REF",
            "network.network_ref",
            "Existing networks require a versioned reference.",
        )
    if network.mode == "CREATE" and network.network_ref is not None:
        reject(
            "CONFLICTING_NETWORK_MODE",
            "network.network_ref",
            "Create mode cannot own an existing network reference.",
        )
    if target.provider == "AWS" and network.mode == "CREATE" and network.vpc_cidr is None:
        reject("MISSING_CIDR", "network.vpc_cidr", "A new AWS VPC requires an address range.")
    if target.provider == "GCP" and network.vpc_cidr is not None:
        reject(
            "UNSUPPORTED_FIELD",
            "network.vpc_cidr",
            "GCP address ranges belong to regional subnets, not the VPC.",
        )

    def parse(value, path):
        try:
            result = ip_network(value, strict=True)
            if not isinstance(result, IPv4Network):
                raise ValueError("Unsupported IPv6 CIDR")  # noqa: TRY004
            return result
        except ValueError:
            reject("INVALID_CIDR", path, "Expected a canonical IPv4 network CIDR.")
            return None

    vpc = parse(network.vpc_cidr, "network.vpc_cidr") if network.vpc_cidr else None
    existing = [
        parse(cidr, f"network.existing_cidrs.{i}") for i, cidr in enumerate(network.existing_cidrs)
    ]
    ranges = []
    for i, subnet in enumerate(network.subnets):
        path = f"network.subnets.{i}"
        cidr = parse(subnet.cidr, f"{path}.cidr")
        if target.provider == "AWS":
            if not subnet.zone or not subnet.zone.startswith(target.region):
                reject(
                    "INVALID_TOPOLOGY",
                    f"{path}.zone",
                    "AWS subnets require an AZ in the selected region.",
                )
            if subnet.region is not None:
                reject("UNSUPPORTED_FIELD", f"{path}.region", "Use an AZ for an AWS subnet.")
        else:
            if subnet.region != target.region or subnet.zone is not None:
                reject(
                    "INVALID_TOPOLOGY", path, "GCP subnets require the selected region and no AZ."
                )
        if cidr is None:
            continue
        if vpc is not None and not cidr.subnet_of(vpc):
            reject(
                "CIDR_OUTSIDE_VPC",
                f"{path}.cidr",
                "Subnet is outside the supplied VPC address range.",
            )
        if any(cidr.overlaps(other) for other in ranges):
            reject("CIDR_OVERLAP", f"{path}.cidr", "Subnet overlaps another requested subnet.")
        # existing_cidrs describes foreign/occupied ranges, excluding selected reused subnets.
        if any(other is not None and cidr.overlaps(other) for other in existing):
            reject(
                "CIDR_OVERLAP", f"{path}.cidr", "Subnet overlaps a supplied occupied address range."
            )
        ranges.append(cidr)
    if (
        network.mode == "CREATE"
        and vpc is not None
        and any(other is not None and vpc.overlaps(other) for other in existing)
    ):
        reject(
            "CIDR_OVERLAP",
            "network.vpc_cidr",
            "New VPC overlaps a supplied occupied address range.",
        )
    return issues


def adapt(spec: ProvisioningSpec) -> ProviderConfiguration:
    issues = validate_target(spec)
    if issues:
        raise ProvisioningInputError(issues)
    target = spec.target
    kinds = {
        "AWS": ("EKS", "AWS_VPC", "ALB"),
        "GCP": ("GKE", "GCP_VPC", "GKE_INGRESS"),
        "ON_PREM": ("K3s", "EXISTING_K3S", "EXISTING_K3S"),
    }
    cluster, network, ingress = kinds[target.provider]
    nodes = target.nodes if target.provider != "ON_PREM" else None
    return ProviderConfiguration(
        provider=target.provider,
        cluster_kind=cluster,
        network_kind=network,
        ingress_kind=ingress,
        node_count=nodes.count if nodes else None,
        node_type=nodes.instance_type if nodes else None,
        node_disk_gib=nodes.disk_gib if nodes else None,
        database_pvc_gib=spec.requirements.database.storage_gib,
        application_port=spec.requirements.application.port,
        image_uri=spec.image.uri,
    )
