"""HTTP routes an application registers (ticket 27). Line-based like the other Go heuristics.

It only records method, path, file and line. A route built at runtime is not visible here.
"""

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_ROUTE = re.compile(
    r'\.(?:HandleFunc|Handle)\(\s*"(?:(GET|POST|PUT|PATCH|DELETE|HEAD)\s+)?(/[^"\s]*)"'
)
_SKIP_DIRS = {"vendor", ".git", "node_modules", "testdata"}


class HttpRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str | None  # None: the route answers every method
    path: str
    file: str
    line: int


def scan_go_routes(root: Path | str) -> list[HttpRoute]:
    base = Path(root)
    found: list[HttpRoute] = []
    for path in sorted(base.rglob("*.go")):
        relative = path.relative_to(base)
        if _SKIP_DIRS.intersection(relative.parts) or path.name.endswith("_test.go"):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        for number, raw in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
            if raw.strip().startswith("//"):
                continue
            match = _ROUTE.search(raw)
            if match:
                found.append(
                    HttpRoute(
                        method=match.group(1),
                        path=match.group(2),
                        file=relative.as_posix(),
                        line=number,
                    )
                )
    return sorted(found, key=lambda r: (r.path, r.method or "", r.file, r.line))
