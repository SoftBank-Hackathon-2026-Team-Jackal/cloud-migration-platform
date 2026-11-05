"""Package a reviewed local Helm Chart into exact `.tgz` bytes (ticket 18, promoted from tests)."""

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue


def package_chart(chart_dir: Path, *, helm_bin: str | None = None) -> bytes:
    """Return `helm package` output bytes. Errors carry a code only, never Helm output."""
    helm = helm_bin or os.environ.get("HELM_BIN", "helm")
    with TemporaryDirectory(prefix="cmp-chart-") as out:
        try:
            subprocess.run(
                [helm, "package", str(chart_dir), "--destination", out],
                capture_output=True,
                check=True,
                timeout=60,
            )
            [path] = Path(out).glob("*.tgz")
            return path.read_bytes()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ProvisioningInputError(
                [
                    ValidationIssue(
                        code="CHART_PACKAGE_FAILED",
                        path="chart",
                        message="Helm Chart could not be packaged.",
                    )
                ]
            ) from None
