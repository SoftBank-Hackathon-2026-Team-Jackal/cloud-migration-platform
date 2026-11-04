"""Ticket 11: deploy approved PostgreSQL + App workloads to a ticket-10 GKE cluster.

`execute_gcp_workloads` runs the shared approved-workload flow (`k8s_workloads`, decisions
K1~K12 in tickets/provisioning/11) with the GCP pieces: gcloud project and cluster status
checks, a Persistent Disk CSI StorageClass, a short-lived ECR pull Secret linked to the
namespace default ServiceAccount, and a GCE Ingress (external Application Load Balancer).
No AWS Load Balancer Controller is involved.

The result holds codes and non-secret references only. The ECR token, DB password, kubeconfig
and command output never enter the result, logs or Temporal history.
"""

import base64
import json
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from temporalio import activity

from cmp_worker.activities import k8s_workloads
from cmp_worker.activities.k8s_workloads import (
    MANAGED_BY,
    Approved,
    ApprovedWorkloads,
    ClusterSession,
    WorkloadFailure,
    ingress_backend,
)

ACTIVITY_NAME = "execute_gcp_workloads"
PULL_SECRET = "cmp-ecr-pull"
STORAGE_PROVISIONER = "pd.csi.storage.gke.io"
ECR = re.compile(
    r"^(?P<registry>(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com)/"
)


@dataclass(frozen=True)
class GkeTarget:
    """Non-secret identifiers of the ticket-10 cluster (resource_refs of the infra run)."""

    project_id: str
    cluster_name: str
    location: str


@dataclass(frozen=True)
class Timeouts(k8s_workloads.Timeouts):
    # GCE load balancer programming and NEG health usually take several minutes (K6).
    ingress_seconds: float = 900


class GkeClusterSession(ClusterSession, Protocol):
    def apply_secret(self, namespace: str, name: str, secret_type: str, data: dict) -> None: ...
    def ingress_address(self, namespace: str, name: str) -> str | None: ...


class GcpWorkloadRuntime(Protocol):
    def project_id(self) -> str: ...
    def cluster_status(self, target: GkeTarget) -> str: ...
    def aws_account_id(self) -> str: ...
    def ecr_password(self, region: str) -> str: ...
    def connect(self, target: GkeTarget) -> AbstractContextManager[GkeClusterSession]: ...
    def http_get_ok(self, url: str) -> bool: ...


# ticket-10 result reference name for each GkeTarget field (10 decision G4).
INFRA_REF_KEYS = {
    "project_id": "gcp_project_id",
    "cluster_name": "gke_cluster_name",
    "location": "gke_location",
}


def target_from_refs(refs: dict, region: str) -> GkeTarget | None:
    values = {field: refs.get(key) for field, key in INFRA_REF_KEYS.items()}
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return GkeTarget(**values)


class ExecutionRefsResolver(k8s_workloads.ExecutionRefsResolver):
    """K12: ticket-10 GKE refs (`gcp_project_id`, `gke_cluster_name`, `gke_location`)."""

    from_refs = staticmethod(target_from_refs)


def storage_class_manifest(name: str) -> dict:
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": name, "labels": MANAGED_BY},
        "provisioner": STORAGE_PROVISIONER,
        "parameters": {"type": "pd-balanced"},
        "reclaimPolicy": "Retain",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
    }


def ingress_manifest(app: dict) -> dict:
    """K6: fixed GCE Ingress derived only from approved App Values. HTTP:80, no TLS (K5)."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": app["release_name"],
            "namespace": app["namespace"],
            "labels": MANAGED_BY,
            "annotations": {"kubernetes.io/ingress.class": "gce"},
        },
        "spec": ingress_backend(app),
    }


def neg_annotation(app: dict) -> dict:
    """Annotation-only server-side apply; the App chart keeps ownership of the Service spec."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": app["release_name"],
            "namespace": app["namespace"],
            "annotations": {"cloud.google.com/neg": json.dumps({"ingress": True})},
        },
    }


def ecr_registries(approved: Approved) -> dict[str, tuple[str, str]]:
    """ECR registry host → (account, region) for every approved image that needs auth."""
    found = {}
    for uri in (approved.db_values.image.uri, approved.app_values["image"]["uri"]):
        match = ECR.match(uri)
        if match:
            found[match["registry"]] = (match["account"], match["region"])
    return found


class GcpWorkloadsActivities(ApprovedWorkloads):
    provider = "GCP"
    activity_name = ACTIVITY_NAME
    storage_provisioner = STORAGE_PROVISIONER

    @staticmethod
    def default_timeouts() -> Timeouts:
        return Timeouts()

    def check_target(self, manifest, target: GkeTarget) -> None:
        if target.project_id != manifest.target.account_id:
            raise WorkloadFailure("PROJECT_MISMATCH")
        region = manifest.target.region
        if target.location != region and not target.location.startswith(f"{region}-"):
            raise WorkloadFailure("REGION_MISMATCH")

    async def check_identity(self, approved: Approved) -> None:
        try:
            project = await self._call(self._runtime.project_id)
        except Exception:  # noqa: BLE001
            raise WorkloadFailure("GCP_IDENTITY_UNAVAILABLE") from None
        if project != approved.manifest.target.account_id:
            raise WorkloadFailure("PROJECT_MISMATCH")
        try:
            status = await self._call(self._runtime.cluster_status, approved.target)
        except Exception:  # noqa: BLE001
            raise WorkloadFailure("CLUSTER_ACCESS_FAILED") from None
        if status != "RUNNING":
            raise WorkloadFailure("CLUSTER_NOT_RUNNING")
        accounts = {account for account, _ in ecr_registries(approved).values()}
        if accounts:
            try:
                account = await self._call(self._runtime.aws_account_id)
            except Exception:  # noqa: BLE001
                raise WorkloadFailure("AWS_IDENTITY_UNAVAILABLE") from None
            if accounts != {account}:
                raise WorkloadFailure("ECR_ACCOUNT_MISMATCH")

    def target_refs(self, target: GkeTarget) -> dict[str, str]:
        return {
            "gcp_project_id": target.project_id,
            "gke_cluster_name": target.cluster_name,
            "gke_location": target.location,
        }

    def storage_class_manifest(self, name: str) -> dict:
        return storage_class_manifest(name)

    async def prepare_namespace(self, cluster, approved: Approved, refs) -> None:
        """K4: short-lived ECR pull Secret; the token lives only in this call's memory."""
        registries = ecr_registries(approved)
        if not registries:
            return
        namespace = approved.db_values.namespace
        try:
            auths = {}
            for registry, (_, region) in sorted(registries.items()):
                token = await self._call(self._runtime.ecr_password, region)
                auth = base64.b64encode(f"AWS:{token}".encode()).decode()
                auths[registry] = {"username": "AWS", "password": token, "auth": auth}
            data = {".dockerconfigjson": json.dumps({"auths": auths})}
            await self._call(
                cluster.apply_secret,
                namespace,
                PULL_SECRET,
                "kubernetes.io/dockerconfigjson",
                data,
            )
            del auths, data, token, auth
            await self._call(
                cluster.apply,
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "default", "namespace": namespace},
                    "imagePullSecrets": [{"name": PULL_SECRET}],
                },
            )
        except Exception:  # noqa: BLE001 - token or CLI output is never surfaced
            raise WorkloadFailure("IMAGE_PULL_SECRET_FAILED") from None
        refs["image_pull_secret"] = PULL_SECRET

    async def expose(self, cluster, approved: Approved, refs) -> None:
        app = approved.app_values
        await self._call(cluster.apply, neg_annotation(app))
        await self._call(cluster.apply, ingress_manifest(app))
        refs["ingress"] = f"{app['namespace']}/{app['release_name']}"
        await self.wait_external(
            cluster,
            approved,
            refs,
            address=lambda c, namespace, name: c.ingress_address(namespace, name),
            codes=("ingress_ip", "INGRESS_ADDRESS_PENDING", "INGRESS_UNREACHABLE"),
            seconds=self._timeouts.ingress_seconds,
        )

    @activity.defn(name=ACTIVITY_NAME)
    async def execute_gcp_workloads(self, request: dict) -> dict:
        return await self._execute(request)
