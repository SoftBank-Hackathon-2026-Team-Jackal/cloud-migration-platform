"""Where the repository resources (Terraform templates, Helm charts, catalog, skills) live.

Installed packages sit in site-packages, so a path derived from `__file__` points outside the
repository inside the worker image. `CMP_RESOURCE_ROOT` names the directory explicitly (ticket 43);
without it the source tree that holds this package is used (local development and tests).
"""

import os
from pathlib import Path

ENV_NAME = "CMP_RESOURCE_ROOT"
# Directories the worker reads at runtime, relative to the resource root. The image must copy each.
RUNTIME_RESOURCE_DIRS = (
    "infra/terraform/aws",
    "infra/terraform/gcp",
    "infra/helm/postgres",
    "infra/helm/application",
    "catalog",
    "skills",
)


def resource_root() -> Path:
    configured = os.environ.get(ENV_NAME)
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise ValueError(f"{ENV_NAME} must be an absolute path")
        return root
    # packages/providers/src/cmp_providers/resources.py → repository root
    return Path(__file__).resolve().parents[4]
