"""kubectl/Helm/gcloud/AWS CLI adapter for ticket 11. Raises code-only errors; never echoes output.

gcloud always runs with the explicit configuration (`CLOUDSDK_ACTIVE_CONFIG_NAME` and
`--configuration`), AWS with the explicit profile (AGENTS.md). kubeconfig is written by
`gcloud container clusters get-credentials` into a 0700 temporary directory (never
`~/.kube/config`) and removed when the run ends; `gke-gcloud-auth-plugin` must be installed.
The ECR token is returned to the caller in memory and reaches the cluster only via stdin.
"""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from cmp_worker.activities import k8s_workloads_cli
from cmp_worker.activities.gcp_workloads import GkeTarget
from cmp_worker.activities.k8s_workloads_cli import FIELD_MANAGER, CommandRunner


class GcpCliRuntime(CommandRunner):
    def __init__(
        self,
        *,
        kubectl: str = "kubectl",
        helm: str = "helm",
        gcloud: str = "gcloud",
        aws: str = "aws",
        gcloud_configuration: str | None = None,
        aws_profile: str | None = None,
    ):
        env = {}
        if gcloud_configuration:
            env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = gcloud_configuration
        if aws_profile:
            env["AWS_PROFILE"] = aws_profile
        super().__init__(kubectl=kubectl, helm=helm, env=env)
        self.gcloud, self.aws = gcloud, aws
        self._configuration = gcloud_configuration

    def _gcloud(self, *args: str, env: dict | None = None) -> bytes:
        scope = ["--configuration", self._configuration] if self._configuration else []
        return self.run([self.gcloud, *args, *scope], env=env)

    def project_id(self) -> str:
        return self._gcloud("config", "get-value", "project").decode().strip()

    def cluster_status(self, target: GkeTarget) -> str:
        out = self._gcloud(
            "container", "clusters", "describe", target.cluster_name,
            "--project", target.project_id, "--location", target.location,
            "--format", "value(status)",
        )  # fmt: skip
        return out.decode().strip()

    def aws_account_id(self) -> str:
        out = self.run(
            [self.aws, "sts", "get-caller-identity", "--query", "Account", "--output", "text"]
        )
        return out.decode().strip()

    def ecr_password(self, region: str) -> str:
        return (
            self.run([self.aws, "ecr", "get-login-password", "--region", region]).decode().strip()
        )

    @contextmanager
    def connect(self, target: GkeTarget) -> Iterator["GkeKubectlSession"]:
        with TemporaryDirectory(prefix="cmp-gke-") as tmp:
            workdir = Path(tmp)
            workdir.chmod(0o700)
            kubeconfig = workdir / "kubeconfig"
            self._gcloud(
                "container", "clusters", "get-credentials", target.cluster_name,
                "--project", target.project_id, "--location", target.location,
                env={"KUBECONFIG": str(kubeconfig)},
            )  # fmt: skip
            if kubeconfig.exists():
                kubeconfig.chmod(0o600)
            yield GkeKubectlSession(self, kubeconfig, workdir)


class GkeKubectlSession(k8s_workloads_cli.KubectlSession):
    def apply_secret(self, namespace: str, name: str, *args) -> None:
        """Create or refresh via stdin server-side apply (managedFields hold names, not values).

        `(namespace, name, secret_type, data)` for the registry pull Secret, or the shared
        `ClusterSession` form `(namespace, name, data)` (Opaque; ticket 19 URL, ENV-04 env)."""
        secret_type, data = args if len(args) == 2 else ("Opaque", args[0])
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": FIELD_MANAGER},
            },
            "type": secret_type,
            "stringData": data,
        }
        self._kubectl(
            "apply", "--server-side", f"--field-manager={FIELD_MANAGER}", "-f", "-",
            stdin=json.dumps(manifest).encode(),
        )  # fmt: skip

    def ingress_address(self, namespace: str, name: str) -> str | None:
        entry = self.ingress_status(namespace, name)
        return (entry.get("ip") or entry.get("hostname")) if entry else None


def build_gcp_workloads_activities(session_factory):
    """Worker wiring; reuses ticket 08/09 variables plus the explicit gcloud configuration."""
    from cmp_api.env_store import optional_env_file_store

    from cmp_worker.activities.gcp_workloads import ExecutionRefsResolver, GcpWorkloadsActivities

    root = os.environ.get("PROVISIONING_ARTIFACT_ROOT")
    runtime = GcpCliRuntime(
        kubectl=os.environ.get("KUBECTL_BIN", "kubectl"),
        helm=os.environ.get("HELM_BIN", "helm"),
        gcloud=os.environ.get("GCLOUD_BIN", "gcloud"),
        aws=os.environ.get("AWS_BIN", "aws"),
        gcloud_configuration=os.environ.get("PROVISIONING_GCLOUD_CONFIGURATION"),
        aws_profile=os.environ.get("PROVISIONING_AWS_PROFILE"),
    )
    return GcpWorkloadsActivities(
        session_factory,
        artifact_root=Path(root) if root else None,
        resolver=ExecutionRefsResolver(),
        runtime=runtime,
        env_store=optional_env_file_store(),  # ENV-04: platform `.env` store (API settings)
    )
