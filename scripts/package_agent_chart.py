"""Package the on-premises Agent Chart for a release (ticket 15, #69; H4).

Copies `infra/helm/onprem-agent`, sets the Chart version/appVersion and pins `image.digest`, then
runs `helm package`. Used by `.github/workflows/agent-release.yml` and runnable locally:

    uv run python scripts/package_agent_chart.py --version 0.1.0 \
        --image-digest sha256:<64 hex> --out dist/
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "infra/helm/onprem-agent"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


def package(version: str, image_digest: str, out: Path, repository: str | None = None) -> Path:
    if not SEMVER.fullmatch(version):
        raise ValueError("INVALID_VERSION")
    if not DIGEST.fullmatch(image_digest):
        raise ValueError("INVALID_IMAGE_DIGEST")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        chart = Path(directory) / "onprem-agent"
        shutil.copytree(CHART, chart)
        manifest = chart / "Chart.yaml"
        text = manifest.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^version: .*$", f"version: {version}", text)
        text = re.sub(r"(?m)^appVersion: .*$", f'appVersion: "{version}"', text)
        manifest.write_text(text, encoding="utf-8")
        values = chart / "values.yaml"
        text = values.read_text(encoding="utf-8")
        text, pinned = re.subn(r'(?m)^  digest: ""$', f'  digest: "{image_digest}"', text)
        if pinned != 1:
            raise ValueError("VALUES_LAYOUT_CHANGED")
        if repository:
            text, replaced = re.subn(r"(?m)^  repository: .*$", f"  repository: {repository}", text)
            if replaced != 1:
                raise ValueError("VALUES_LAYOUT_CHANGED")
        values.write_text(text, encoding="utf-8")
        helm = os.environ.get("HELM_BIN", "helm")
        subprocess.run(
            [helm, "package", str(chart), "--destination", str(out)],
            check=True,
            capture_output=True,
        )
    return out / f"onprem-agent-{version}.tgz"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--repository")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        path = package(args.version, args.image_digest, args.out, args.repository)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
