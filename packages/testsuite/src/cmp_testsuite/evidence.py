"""Evidence check for AI tests (ticket 27-B).

A test is only as good as what it was written from, so every AI test cites the source lines it
came from, and this check looks at the real files. It does not decide whether the test is right;
it refuses tests whose citation does not exist or does not mention the routes the test calls.
"""

from pathlib import Path

from cmp_testsuite.model import Evidence, HttpStep, SuiteCase

__all__ = ["Evidence", "check_evidence", "read_source_lines"]

# A handler registration and the route string it names sit on the same or a nearby line.
_WINDOW = 2


def check_evidence(workspace: Path | str, case: SuiteCase) -> str | None:
    """None when the citations hold, else a code for the first problem (never file content)."""
    if not case.evidence:
        return "NO_EVIDENCE"
    root = Path(workspace).resolve()
    windows: list[str] = []
    for item in case.evidence:
        lines = read_source_lines(root, item.file)
        if lines is None:
            return "EVIDENCE_FILE_MISSING"
        if item.line > len(lines):
            return "EVIDENCE_LINE_MISSING"
        low, high = max(0, item.line - 1 - _WINDOW), item.line + _WINDOW
        windows.append("\n".join(lines[low:high]))
    for step in case.steps:
        if not isinstance(step, HttpStep):
            continue
        fixed = _fixed_part(step.path)
        if fixed and not any(fixed in text for text in windows):
            return f"EVIDENCE_MISMATCH {fixed}"
    return None


def read_source_lines(root: Path | str, name: str) -> list[str] | None:
    """The lines of a file inside the source; None for anything else (never says why)."""
    root = Path(root).resolve()
    path = (root / name).resolve()
    if root != path and root not in path.parents:
        return None  # outside the source: answered like a missing file
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _fixed_part(path: str) -> str:
    """The part of a step path that is written out in the source, without a trailing slash."""
    return path.split("{{", 1)[0].rstrip("/")
