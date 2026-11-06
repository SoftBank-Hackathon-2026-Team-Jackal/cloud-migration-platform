"""Look at and clean up Sandbox namespaces on the dedicated k3d cluster (`k3d-cmp-sandbox` only).

  uv run python scripts/sandbox_ctl.py status
  uv run python scripts/sandbox_ctl.py cleanup                       # expired ones only
  uv run python scripts/sandbox_ctl.py cleanup --all --confirm DELETE-ALL
  uv run python scripts/sandbox_ctl.py prewarm                       # pull the shared images onto the node

To run the cleanup on a schedule (macOS launchd or cron), call `cleanup` every 15 minutes.
It prints only namespaces, run IDs and expiry times, never a secret.
"""

import sys
from pathlib import Path

from cmp_worker.activities.k8s_workloads_cli import CommandRunner
from cmp_worker.config import settings
from cmp_worker.sandbox_deploy import SANDBOX_CONTEXT, SandboxCluster, SandboxService
from cmp_worker.sandbox_ops import prewarm_images, run_command


def main(argv: list[str]) -> int:
    if argv == ["prewarm"]:  # needs docker and the node, not the cluster's kubeconfig
        for image in prewarm_images(CommandRunner()):
            print(f"받아 둠: {image}")
        return 0
    if not settings.sandbox_kubeconfig:
        print("SANDBOX_KUBECONFIG가 설정되지 않았습니다.")
        return 2
    kubeconfig = Path(settings.sandbox_kubeconfig).expanduser()
    if not kubeconfig.is_file():
        print("SANDBOX_KUBECONFIG 파일을 찾을 수 없습니다.")
        return 2
    root = Path(__file__).resolve().parents[1]
    service = SandboxService(
        SandboxCluster(CommandRunner(), kubeconfig=kubeconfig, context=SANDBOX_CONTEXT),
        database_chart_dir=root / "infra/helm/postgres",
        application_chart_dir=root / "infra/helm/application",
    )
    code, text = run_command(argv, service)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
