"""Demo mock of the red-team attack and its security fix (`SandboxSettings.redteam_mock`).

When the switch is on, `sbx_redteam` and `sbx_fix` replay what one real run recorded instead of
calling a model: the first attack returns that run's open Findings, the re-check on the fixed image
resolves them all, and the security fix applies the code change the real AI Fixer made. The
Workflow is not touched, so the screen sees the same steps, branches and events as a real run.

Nothing here is invented: `redteam_mock/round1.json` is the first `sbx_redteam` result of the real
run `migration-26c7a25b-51f0-4c80-be7f-6cffb854c016` (from its Temporal history) and
`redteam_mock/fix.json` the code change its `sbx_fix` made, as a unified diff against that run's
source (demo-app 627d1b7); both were read from the demo server, read-only, and hold no secrets.
Values that belong to one run (image digest, suite hash, time, evidence references) are replaced
with the current run's own.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from cmp_agents.fixer import CodeFix, FileChange

from cmp_worker.artifact_store import ArtifactStore
from cmp_worker.redteam_safeguards import EVIDENCE_KIND, Finding, summarize

FIXTURE_DIR = Path(__file__).parent / "redteam_mock"
FIRST_WAIT_SECONDS = 25.0  # what a real first attack takes on the demo app, give or take
RECHECK_WAIT_SECONDS = 5.0

# Why the mock fix was not used (logged; the real Fixer then runs as usual).
FIX_FIXTURE_MISSING = "REDTEAM_MOCK_FIX_FIXTURE_MISSING"
FIX_SOURCE_CHANGED = "REDTEAM_MOCK_SOURCE_CHANGED"
FIX_NO_WORKSPACE = "REDTEAM_MOCK_NO_WORKSPACE"


def load(name: str, root: Path | None = None) -> dict[str, Any] | None:
    path = (root or FIXTURE_DIR) / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def first_report(
    store: ArtifactStore,
    *,
    digest: str,
    suite_sha256: str,
    now: datetime,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    """The recorded first attack, re-stamped for this image and suite (P-10).

    Evidence text kept in the fixture is stored again here, so a Finding never points at an
    artifact of another run or another server."""
    recorded = fixture["result"]
    evidence = fixture.get("evidence") or {}
    found: list[Finding] = []
    repros: dict[str, dict] = {}
    for item in recorded["open"]:
        fid = item["finding_id"]
        text = evidence.get(fid)
        ref = (
            store.put(text.encode(), ref_id=f"{EVIDENCE_KIND}-{fid}", kind=EVIDENCE_KIND)
            if text is not None
            else None
        )
        fields = {k: v for k, v in item.items() if k != "repro"}
        found.append(
            Finding.model_validate(
                {
                    **fields,
                    "evidence_ref": ref.model_dump(mode="json") if ref else None,
                    "image_digest": digest,
                    "suite_sha256": suite_sha256,
                    "executed_at": now.isoformat(),
                }
            )
        )
        repros[fid] = item["repro"]
    return {
        "status": "DONE", "code": None,
        "open": [{**f.model_dump(mode="json"), "repro": repros[f.finding_id]} for f in found],
        "resolved": [], "withheld": [], "model_error": None,
        "summary": summarize(found).model_dump(mode="json"),
        "stopped_reason": recorded.get("stopped_reason"),
    }  # fmt: skip


def recheck_report(previous: list[dict]) -> dict[str, Any]:
    """The re-check on the fixed image: every earlier Finding is gone (as in the real run)."""
    return {
        "status": "DONE", "code": None, "open": [],
        "resolved": [f["finding_id"] for f in previous], "withheld": [], "model_error": None,
        "summary": summarize([]).model_dump(mode="json"), "stopped_reason": None,
    }  # fmt: skip


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def parse_patch(text: str) -> dict[str, list[tuple[int, list[str], list[str]]]]:
    """A unified diff as {path: [(old_start, old_lines, new_lines), ...]} (`a/`/`b/` prefixes)."""
    files: dict[str, list[tuple[int, list[str], list[str]]]] = {}
    hunks: list[tuple[int, list[str], list[str]]] | None = None
    for line in text.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0]
            hunks = files.setdefault(path.removeprefix("b/"), [])
            continue
        match = _HUNK.match(line)
        if match and hunks is not None:
            hunks.append((int(match.group(1)), [], []))
            continue
        if not hunks or line.startswith("\\"):
            continue
        _, old, new = hunks[-1]
        tag, body = line[:1], line[1:]
        if tag in (" ", ""):
            old.append(body)
            new.append(body)
        elif tag == "-":
            old.append(body)
        elif tag == "+":
            new.append(body)
    return files


def _apply_hunks(text: str, hunks: list[tuple[int, list[str], list[str]]]) -> str | None:
    """Apply hunks exactly (no fuzz; a shifted position is fine). None when one does not fit."""
    lines = text.split("\n")
    out: list[str] = []
    pos = 0
    for start, old, new in hunks:
        want = max(start - 1, 0)
        at = next(
            (
                i
                for i in sorted(range(pos, len(lines) - len(old) + 1), key=lambda i: abs(i - want))
                if lines[i : i + len(old)] == old
            ),
            None,
        )
        if at is None:
            return None
        out += lines[pos:at] + new
        pos = at + len(old)
    return "\n".join(out + lines[pos:])


def code_fix(workspace: str | None, fixture: dict[str, Any] | None) -> tuple[CodeFix | None, str]:
    """The recorded fix as a `CodeFix` on this source, or why it does not fit.

    The recorded patch is applied exactly to the current files; if any hunk no longer matches,
    the source has changed since the recording and the change is not applied."""
    if fixture is None:
        return None, FIX_FIXTURE_MISSING
    if not workspace or not Path(workspace).is_dir():
        return None, FIX_NO_WORKSPACE
    root = Path(workspace)
    changes = []
    for path, hunks in parse_patch(fixture["patch"]).items():
        target = root / path
        if not target.is_file():
            return None, FIX_SOURCE_CHANGED
        fixed = _apply_hunks(target.read_text(encoding="utf-8"), hunks)
        if fixed is None:
            return None, FIX_SOURCE_CHANGED
        changes.append(FileChange(path=path, content=fixed))
    if not changes:
        return None, FIX_FIXTURE_MISSING
    return CodeFix(reason=fixture["reason"], files=changes), ""
