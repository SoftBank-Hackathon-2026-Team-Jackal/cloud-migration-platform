"""Turn the AWS scan and code scan into `network_intent` entries and external-entry records.

Intents hold resource IDs, hosts and ports only: no CIDRs, security group IDs, load balancer
kinds (AV-005). Entries the contract has no place for (internet-to-app, source CIDRs) go to
the analysis result instead (D-15). Anything that cannot be mapped safely is noted, not guessed.
"""

from collections.abc import Sequence
from typing import Literal

from cmp_contracts.analysis import ExternalEntry
from cmp_contracts.provisioning_v2 import NetworkIntent
from pydantic import BaseModel, ConfigDict

from cmp_scanners.aws_infra import AwsInfraScan, SecurityGroupInfo
from cmp_scanners.code import CodeScan

_INTERNET = {"0.0.0.0/0", "::/0"}
_CLUSTER_LOCAL_SUFFIXES = (".cluster.local", ".svc")


class NetworkAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intents: list[NetworkIntent]
    external_entries: list[ExternalEntry]
    source_cidrs: list[str]
    notes: list[str]


def merge_network_intents(*groups: Sequence[NetworkIntent]) -> list[NetworkIntent]:
    seen: dict[tuple, NetworkIntent] = {}
    for group in groups:
        for intent in group:
            key = (
                intent.source_resource_id,
                intent.destination_resource_id,
                intent.external_host,
                intent.port,
                intent.protocol,
            )
            seen.setdefault(key, intent)
    return list(seen.values())


def _resource_for(group: SecurityGroupInfo, candidates: Sequence[str]) -> str | None:
    text = " ".join([group.name, *group.tags.values()]).lower()
    matches = [c for c in candidates if c.lower() in text]
    return max(matches, key=len) if matches else None


def _single_port(rule) -> int | None:
    if rule.protocol not in {"tcp", "udp"} or rule.from_port is None:
        return None
    return rule.from_port if rule.from_port == rule.to_port else None


def build_network(
    infra: AwsInfraScan | None,
    code: CodeScan,
    *,
    application_ids: Sequence[str],
    database_ids: Sequence[str],
) -> NetworkAnalysis:
    candidates = [*application_ids, *database_ids]
    notes: list[str] = []
    if infra is None:
        notes.append("INFRA_NOT_COLLECTED: security groups, load balancers and CIDRs were not read")
    groups = infra.security_groups if infra else []
    balancers = infra.load_balancers if infra else []
    owner = {g.id: _resource_for(g, candidates) for g in groups}
    intents: list[NetworkIntent] = []
    entries: list[ExternalEntry] = []

    for group in groups:
        destination = owner[group.id]
        for rule in group.ingress:
            if not rule.source_group_ids:
                continue
            if destination is None:
                notes.append(f"SECURITY_GROUP_UNMAPPED: {group.name}")
                continue
            port = _single_port(rule)
            if port is None:
                notes.append(f"BROAD_SECURITY_GROUP_RULE: {group.name}")
                continue
            for source_group in rule.source_group_ids:
                source = owner.get(source_group)
                if source and source != destination:
                    intents.append(
                        NetworkIntent(
                            source_resource_id=source,
                            destination_resource_id=destination,
                            port=port,
                            protocol=rule.protocol.upper(),
                        )
                    )

    covered: set[str] = set()
    for balancer in balancers:
        if balancer.scheme != "internet-facing":
            continue
        by_name = [a for a in application_ids if a.lower() in balancer.name.lower()]
        targets = list(application_ids) if len(application_ids) == 1 else by_name
        web = [
            (l, p)
            for l in balancer.listeners
            if l.protocol in {"HTTP", "HTTPS"}
            for p in l.target_ports
        ]
        if not web:
            continue
        if len(targets) != 1:
            notes.append(f"EXTERNAL_ENTRY_TARGET_AMBIGUOUS: {balancer.name}")
            continue
        for listener, target_port in web:
            protocol: Literal["HTTP", "HTTPS"] = "HTTPS" if listener.protocol == "HTTPS" else "HTTP"
            entries.append(
                ExternalEntry(
                    protocol=protocol,
                    port=listener.port,
                    target_resource_id=targets[0],
                    target_port=target_port,
                    source_service="ALB",
                )
            )
            covered.add(targets[0])

    for group in groups:
        resource = owner[group.id]
        if resource not in application_ids or resource in covered:
            continue
        for rule in group.ingress:
            port = _single_port(rule)
            if port is not None and _INTERNET.intersection(rule.cidrs):
                entries.append(
                    ExternalEntry(
                        protocol="HTTPS" if port == 443 else "HTTP",
                        port=port,
                        target_resource_id=resource,
                        target_port=port,
                        source_service="SECURITY_GROUP",
                    )
                )

    for site in code.call_sites:
        if site.kind != "EXTERNAL_HOST":
            continue
        host, _, explicit = site.target.partition(":")
        if host.endswith(_CLUSTER_LOCAL_SUFFIXES):
            continue
        port = int(explicit) if explicit.isdigit() else (80 if site.detail == "http" else 443)
        for app in application_ids:
            intents.append(
                NetworkIntent(source_resource_id=app, external_host=host, port=port, protocol="TCP")
            )

    return NetworkAnalysis(
        intents=merge_network_intents(intents),
        external_entries=[e for i, e in enumerate(entries) if e not in entries[:i]],
        source_cidrs=sorted({c for vpc in (infra.vpcs if infra else []) for c in vpc.cidrs}),
        notes=list(dict.fromkeys(notes)),
    )
