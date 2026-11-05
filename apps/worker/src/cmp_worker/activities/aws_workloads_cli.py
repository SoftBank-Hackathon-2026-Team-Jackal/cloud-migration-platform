"""kubectl/Helm/AWS CLI adapter for ticket 09. Raises code-only errors; never echoes output.

The generic kubectl/Helm session lives in `k8s_workloads_cli`; this module adds the EKS
kubeconfig, STS account lookup and the pinned AWS Load Balancer Controller install.
"""

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from cmp_providers.artifacts import canonical_json_bytes

from cmp_worker.activities import k8s_workloads_cli
from cmp_worker.activities.aws_workloads import ClusterTarget
from cmp_worker.activities.k8s_workloads_cli import (
    DB_PROBE,
    FIELD_MANAGER,
    CommandError,
    CommandRunner,
)

__all__ = ["DB_PROBE", "FIELD_MANAGER", "CliRuntime", "CommandError", "KubectlSession"]

LBC_REPO = "https://aws.github.io/eks-charts"
LBC_CHART = "aws-load-balancer-controller"
LBC_VERSION = "3.6.2"
# sha256 of the official eks-charts package for 3.6.2, pinned at implementation (W6).
LBC_SHA256 = "9255bf39873b6cb31106dbe61dc87c3617bff4197783b5b94381811a13f99c3c"
LBC_NAMESPACE = "kube-system"
LBC_SERVICE_ACCOUNT = "aws-load-balancer-controller"


class CliRuntime(CommandRunner):
    def __init__(
        self,
        *,
        kubectl: str = "kubectl",
        helm: str = "helm",
        aws: str = "aws",
        aws_profile: str | None = None,
    ):
        # Explicit named profile (AGENTS.md); never a silent default.
        super().__init__(
            kubectl=kubectl, helm=helm, env={"AWS_PROFILE": aws_profile} if aws_profile else None
        )
        self.aws = aws

    def account_id(self) -> str:
        out = self.run(
            [self.aws, "sts", "get-caller-identity", "--query", "Account", "--output", "text"]
        )
        return out.decode().strip()

    @contextmanager
    def connect(self, target: ClusterTarget) -> Iterator["KubectlSession"]:
        with TemporaryDirectory(prefix="cmp-eks-") as tmp:
            workdir = Path(tmp)
            workdir.chmod(0o700)
            kubeconfig = workdir / "kubeconfig"
            self.run(
                [
                    self.aws,
                    "eks",
                    "update-kubeconfig",
                    "--name",
                    target.cluster_name,
                    "--region",
                    target.region,
                    "--kubeconfig",
                    str(kubeconfig),
                ]
            )
            kubeconfig.chmod(0o600)
            yield KubectlSession(self, kubeconfig, workdir)


class KubectlSession(k8s_workloads_cli.KubectlSession):
    def ensure_lb_controller(self, target: ClusterTarget) -> None:
        folder = self._workdir / "lbc"
        folder.mkdir(mode=0o700)
        self._rt.run(
            [self._rt.helm, "pull", LBC_CHART, "--repo", LBC_REPO, "--version", LBC_VERSION,
             "-d", str(folder)]
        )  # fmt: skip
        chart = folder / f"{LBC_CHART}-{LBC_VERSION}.tgz"
        if hashlib.sha256(chart.read_bytes()).hexdigest() != LBC_SHA256:
            raise CommandError("LB_CONTROLLER_CHART_DIGEST")
        values = {
            "clusterName": target.cluster_name,
            "region": target.region,
            "vpcId": target.vpc_id,
            "serviceAccount": {
                "create": True,
                "name": LBC_SERVICE_ACCOUNT,
                "annotations": {"eks.amazonaws.com/role-arn": target.lb_controller_role_arn},
            },
        }
        current = self.release(LBC_NAMESPACE, LBC_CHART)
        if (
            current is None
            or current["status"] != "deployed"
            or (
                current["chart"] != f"{LBC_CHART}-{LBC_VERSION}"
                or canonical_json_bytes(current["values"]) != canonical_json_bytes(values)
            )
        ):
            self.helm_upgrade_install(LBC_NAMESPACE, LBC_CHART, chart, values)
        self._kubectl(
            "rollout", "status", f"deployment/{LBC_CHART}", "-n", LBC_NAMESPACE,
            "--timeout=300s", timeout=330,
        )  # fmt: skip

    def ingress_hostname(self, namespace: str, name: str) -> str | None:
        entry = self.ingress_status(namespace, name)
        return entry.get("hostname") if entry else None


def build_aws_workloads_activities(session_factory):
    """Worker wiring from the same environment variables ticket 08 uses."""
    from cmp_api.env_store import optional_env_file_store

    from cmp_worker.activities.aws_workloads import AwsWorkloadsActivities, ExecutionRefsResolver

    root = os.environ.get("PROVISIONING_ARTIFACT_ROOT")
    runtime = CliRuntime(
        kubectl=os.environ.get("KUBECTL_BIN", "kubectl"),
        helm=os.environ.get("HELM_BIN", "helm"),
        aws=os.environ.get("AWS_BIN", "aws"),
        aws_profile=os.environ.get("PROVISIONING_AWS_PROFILE"),
    )
    return AwsWorkloadsActivities(
        session_factory,
        artifact_root=Path(root) if root else None,
        resolver=ExecutionRefsResolver(),
        runtime=runtime,
        env_store=optional_env_file_store(),  # ENV-04: platform `.env` store (API settings)
    )
