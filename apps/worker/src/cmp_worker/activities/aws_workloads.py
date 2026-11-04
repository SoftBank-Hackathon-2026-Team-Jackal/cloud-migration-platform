"""Ticket 09: deploy approved PostgreSQL + App workloads to a ticket-08 EKS cluster.

`execute_aws_workloads` runs the shared approved-workload flow (`k8s_workloads`, decisions
W1~W11 in tickets/provisioning/09) with the AWS pieces: STS account check, the AWS Load
Balancer Controller, an EBS gp3 StorageClass and an internet-facing ALB Ingress.

The result holds codes and non-secret references only. Secret values, kubeconfig and command
output never enter the result, logs or Temporal history.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from temporalio import activity

from cmp_worker.activities import k8s_workloads
from cmp_worker.activities.k8s_workloads import (
    CONFIG_FAILURES,
    MANAGED_BY,
    PULL_FAILURES,
    WORKLOAD_ROLES,
    Approved,
    ApprovedWorkloads,
    TargetResolver,
    WorkloadFailure,
    chart_identity,
    ingress_backend,
)
from cmp_worker.activities.k8s_workloads import ClusterSession as _ClusterSession

ACTIVITY_NAME = "execute_aws_workloads"
__all__ = [
    "ACTIVITY_NAME",
    "CONFIG_FAILURES",
    "MANAGED_BY",
    "PULL_FAILURES",
    "WORKLOAD_ROLES",
    "AwsWorkloadsActivities",
    "ClusterSession",
    "ClusterTarget",
    "ExecutionRefsResolver",
    "TargetResolver",
    "Timeouts",
    "WorkloadFailure",
    "WorkloadRuntime",
    "chart_identity",
    "ingress_manifest",
    "storage_class_manifest",
    "target_from_refs",
]


@dataclass(frozen=True)
class ClusterTarget:
    """Non-secret identifiers of the ticket-08 cluster (resource_refs of the infra run)."""

    cluster_name: str
    region: str
    vpc_id: str
    lb_controller_role_arn: str


@dataclass(frozen=True)
class Timeouts(k8s_workloads.Timeouts):
    alb_seconds: float = 600


class ClusterSession(_ClusterSession, Protocol):
    def ensure_lb_controller(self, target: ClusterTarget) -> None: ...
    def ingress_hostname(self, namespace: str, name: str) -> str | None: ...


class WorkloadRuntime(Protocol):
    def account_id(self) -> str: ...
    def connect(self, target: ClusterTarget) -> AbstractContextManager[ClusterSession]: ...
    def http_get_ok(self, url: str) -> bool: ...


# ticket-08 result reference name for each ClusterTarget field (aws_apply.OUTPUT_REFS).
INFRA_REF_KEYS = {
    "cluster_name": "eks_cluster_name",
    "vpc_id": "vpc_id",
    "lb_controller_role_arn": "lb_controller_role_arn",
}


def target_from_refs(refs: dict, region: str) -> ClusterTarget | None:
    values = {field: refs.get(key) for field, key in INFRA_REF_KEYS.items()}
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return ClusterTarget(region=region, **values)


class ExecutionRefsResolver(k8s_workloads.ExecutionRefsResolver):
    """W11: ticket-08 EKS refs (`eks_cluster_name`, `vpc_id`, `lb_controller_role_arn`)."""

    from_refs = staticmethod(target_from_refs)


def storage_class_manifest(name: str) -> dict:
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": name, "labels": MANAGED_BY},
        "provisioner": "ebs.csi.aws.com",
        "parameters": {"type": "gp3", "encrypted": "true"},
        "reclaimPolicy": "Retain",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
    }


def ingress_manifest(app: dict) -> dict:
    """W5: fixed ALB template derived only from approved App Values. HTTP:80, no TLS (W4)."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": app["release_name"],
            "namespace": app["namespace"],
            "labels": MANAGED_BY,
            "annotations": {
                "alb.ingress.kubernetes.io/scheme": "internet-facing",
                "alb.ingress.kubernetes.io/target-type": "ip",
                "alb.ingress.kubernetes.io/listen-ports": '[{"HTTP": 80}]',
                "alb.ingress.kubernetes.io/healthcheck-path": app["application"]["health_path"],
            },
        },
        "spec": {"ingressClassName": "alb", **ingress_backend(app)},
    }


class AwsWorkloadsActivities(ApprovedWorkloads):
    provider = "AWS"
    activity_name = ACTIVITY_NAME
    storage_provisioner = "ebs.csi.aws.com"

    @staticmethod
    def default_timeouts() -> Timeouts:
        return Timeouts()

    def check_target(self, manifest, target: ClusterTarget) -> None:
        if target.region != manifest.target.region:
            raise WorkloadFailure("REGION_MISMATCH")

    async def check_identity(self, approved: Approved) -> None:
        try:
            account = await self._call(self._runtime.account_id)
        except Exception:  # noqa: BLE001
            raise WorkloadFailure("AWS_IDENTITY_UNAVAILABLE") from None
        if account != approved.manifest.target.account_id:
            raise WorkloadFailure("ACCOUNT_MISMATCH")

    def target_refs(self, target: ClusterTarget) -> dict[str, str]:
        return {"cluster_name": target.cluster_name}

    async def prepare_cluster(self, cluster, approved: Approved, refs) -> None:
        try:
            await self._call(cluster.ensure_lb_controller, approved.target)
        except Exception:  # noqa: BLE001 - CLI output is never surfaced
            raise WorkloadFailure("LB_CONTROLLER_NOT_READY") from None
        refs["lb_controller"] = "kube-system/aws-load-balancer-controller"

    def storage_class_manifest(self, name: str) -> dict:
        return storage_class_manifest(name)

    async def expose(self, cluster, approved: Approved, refs) -> None:
        app = approved.app_values
        await self._call(cluster.apply, ingress_manifest(app))
        refs["ingress"] = f"{app['namespace']}/{app['release_name']}"
        await self.wait_external(
            cluster,
            approved,
            refs,
            address=lambda c, namespace, name: c.ingress_hostname(namespace, name),
            codes=("alb_hostname", "ALB_ADDRESS_PENDING", "ALB_UNREACHABLE"),
            seconds=self._timeouts.alb_seconds,
        )

    @activity.defn(name=ACTIVITY_NAME)
    async def execute_aws_workloads(self, request: dict) -> dict:
        return await self._execute(request)
