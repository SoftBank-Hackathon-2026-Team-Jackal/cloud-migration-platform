"""Which AWS resources the app uses: the user's scope, the code and CloudTrail side by side.

Ticket 06C. The scan already reads IAM role policies and CloudTrail; this turns those reads into
evidence in the analysis report and compares them with the candidates the user kept or dropped in
the web scope selection (ticket 56, 06B). The comparison never changes a mapping's grade or
treatment: it only adds risk codes the review can see.

What the traces can and cannot say:
- Code: AWS SDK imports and calls the Go adapter found. Partial when files were not scanned.
- CloudTrail `LookupEvents`: management events of the whole account (other principals and the
  platform's own calls included), no data events, a short history. "No call recorded" is never
  proof that an app did not use a service, and a service seen only there is never reported as
  outside the scope.
"""

from collections.abc import Collection, Iterable

from cmp_contracts.analysis import (
    AwsUsage,
    CloudTrailCall,
    ConfirmedScope,
    Evidence,
    IamRoleUsage,
)

from cmp_scanners.aws_infra import AwsInfraScan, CloudTrailEventSummary
from cmp_scanners.code import CodeScan

# Services the platform and the cluster use themselves; they are not resources an app owns.
INFRASTRUCTURE_SERVICES = frozenset(
    {"iam", "sts", "eks", "ec2", "elasticloadbalancing", "elasticloadbalancingv2", "cloudtrail"}
)
_DATA_EVENTS_NOTE = "CLOUDTRAIL_DATA_EVENTS_NOT_VISIBLE"


def _arn_service(arn: str | None) -> str | None:
    parts = (arn or "").split(":")
    if len(parts) < 6 or parts[0] != "arn" or not parts[2]:
        return None
    return parts[2].lower()


def candidate_service(resource_type: str, arn: str | None) -> str | None:
    """The AWS service of a scope candidate that is an app resource (S3, SQS, RDS, ...).

    None for candidates that are not one (IAM roles, load balancers, Secret and ConfigMap
    references, the in-cluster PostgreSQL), which have no ARN of an app-owned service."""
    service = _arn_service(arn)
    if service is None or service in INFRASTRUCTURE_SERVICES:
        return None
    return service


def code_services(code: CodeScan) -> set[str]:
    """Services the code imports or calls (`s3`, `sqs.SendMessage` -> `s3`, `sqs`)."""
    return {site.target.split(".")[0].lower() for site in code.call_sites if site.kind == "AWS_SDK"}


def trail_services(events: Iterable[CloudTrailEventSummary]) -> set[str]:
    """Services of the events CloudTrail returned (`sqs.amazonaws.com` -> `sqs`)."""
    return {event.event_source.split(".")[0].lower() for event in events}


def reconcile_usage(
    scope: ConfirmedScope | None,
    *,
    code_services: Collection[str] | None,
    trail_services: Collection[str] | None,
) -> list[str]:
    """Risk codes from the user's candidates against the traces. None means a source was not
    available; with no scope or no source there is nothing to compare."""
    if scope is None or (code_services is None and trail_services is None):
        return []
    sources = {"code": code_services, "cloudtrail": trail_services}
    checked = [name for name, seen in sources.items() if seen is not None]
    risks: set[str] = set()
    covered: set[str] = set()
    for item in scope.candidates:
        service = candidate_service(item.resource_type, item.arn)
        if service is None:
            continue
        covered.add(service)
        seen_in = [name for name, seen in sources.items() if seen and service in seen]
        label = f"{item.resource_type} {item.name}"
        if not item.selected and seen_in:
            risks.add(f"SCOPE_EXCLUDED_BUT_USED: {label} (seen in {', '.join(seen_in)})")
        elif item.selected and not seen_in:
            risks.add(
                f"SCOPE_SELECTED_NO_USAGE: {label} "
                f"(checked {', '.join(checked)}; {_DATA_EVENTS_NOTE})"
            )
    # Only the code can say a service is missing from the scope: CloudTrail sees the whole account.
    for service in sorted(set(code_services or ()) - covered - INFRASTRUCTURE_SERVICES):
        risks.add(f"USAGE_NOT_IN_SCOPE: {service} (code)")
    return sorted(risks)


def build_aws_usage(
    status: str, scan: AwsInfraScan | None, *, reason: str | None = None
) -> AwsUsage:
    """The scan's IAM and CloudTrail reads as report evidence; a scan that did not run is
    stated with its reason, never an empty list."""
    if status != "COLLECTED" or scan is None:
        return AwsUsage(status=status, reason=reason)  # type: ignore[arg-type]
    roles = [
        IamRoleUsage(
            role_arn=role.role_arn,
            actions=role.actions,
            wildcard_resources=role.wildcard_resources,
            evidence=Evidence(kind="IAM_POLICY", locator=role.role_arn, verified=True),
        )
        for role in sorted(scan.role_policies, key=lambda r: r.role_arn)
    ]
    calls = [
        CloudTrailCall(
            event_source=event.event_source,
            event_name=event.event_name,
            count=event.count,
            evidence=Evidence(
                kind="CLOUDTRAIL",
                locator=f"{event.event_source}:{event.event_name}",
                verified=True,
            ),
        )
        for event in sorted(scan.cloudtrail, key=lambda e: (e.event_source, e.event_name))
    ]
    return AwsUsage(status="COLLECTED", iam_roles=roles, cloudtrail=calls)


def cloud_resource_locators(usage: AwsUsage) -> set[str]:
    """The locators a mapping row may cite as IAM_POLICY or CLOUDTRAIL evidence."""
    return {
        *(role.evidence.locator for role in usage.iam_roles),
        *(call.evidence.locator for call in usage.cloudtrail),
    }
