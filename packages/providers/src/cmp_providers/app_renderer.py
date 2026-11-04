"""Helm I/O boundary for Activities. Never call from a Temporal Workflow."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from cmp_providers.application import validate_application
from cmp_providers.artifacts import canonical_json_bytes


class ApplicationRenderError(ValueError):
    pass


def render_application(payload: dict, *, chart_dir: Path, helm_bin: str = "helm") -> bytes:
    """Render validated references using a trusted chart path and executable.

    Caller verifies approved Chart/Values digests before calling. No install, lookup,
    credentials, raw config contents, or remote artifact storage is performed here.
    """
    values = validate_application(payload)
    for item in values.env:
        if item.source.kind == "DATABASE":
            field = {
                "secret_ref": "secret_name",
                "connection_url_secret_ref": "url_secret_name",
            }.get(item.source.field, item.source.field)
            if getattr(values.database, field) is None:
                raise ApplicationRenderError("Application bindings are unresolved")
    try:
        with TemporaryDirectory(prefix="cmp-app-render-") as directory:
            path = Path(directory) / "values.json"
            path.write_bytes(canonical_json_bytes(values.model_dump(mode="json")))
            result = subprocess.run(
                [
                    helm_bin,
                    "template",
                    values.release_name,
                    str(chart_dir.resolve()),
                    "--namespace",
                    values.namespace,
                    "--values",
                    str(path),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if result.returncode != 0:
                raise ApplicationRenderError("Application chart rendering failed")
            return result.stdout
    except (OSError, subprocess.SubprocessError):
        # Helm stderr/command exceptions can contain values; do not forward them.
        raise ApplicationRenderError("Application chart rendering failed") from None
