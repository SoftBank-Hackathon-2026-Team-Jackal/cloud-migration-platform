"""Demo mock of the red-team attack and its security fix (`SandboxSettings.redteam_mock`).

When the switch is on, `sbx_redteam` and `sbx_fix` replay what one real run recorded instead of
calling a model: the first attack returns that run's open Findings, the re-check on the fixed image
resolves them all, and the security fix applies the code change the real AI Fixer made. The
Workflow is not touched, so the screen sees the same steps, branches and events as a real run.

Nothing here is invented: `redteam_mock/round1.json` is the first `sbx_redteam` result of the real
run `migration-26c7a25b-51f0-4c80-be7f-6cffb854c016` and `redteam_mock/fix.json` the code change
its `sbx_fix` produced (both read from that run's Temporal history and fix workspace, secrets
removed). Values that belong to one run (image digest, suite hash, time, evidence references) are
replaced with the current run's own.
"""

import hashlib
import json
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


def code_fix(workspace: str | None, fixture: dict[str, Any] | None) -> tuple[CodeFix | None, str]:
    """The recorded fix as a `CodeFix`, or why it does not fit this source.

    Each file must still be exactly what the real Fixer started from (`before_sha256`; `null` for a
    file it created); otherwise the source has changed and the recorded change is not applied."""
    if fixture is None:
        return None, FIX_FIXTURE_MISSING
    if not workspace or not Path(workspace).is_dir():
        return None, FIX_NO_WORKSPACE
    root = Path(workspace)
    for change in fixture["files"]:
        path = root / change["path"]
        before = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if before != change["before_sha256"]:
            return None, FIX_SOURCE_CHANGED
    fix = CodeFix(
        reason=fixture["reason"],
        files=[FileChange(path=c["path"], content=c["content"]) for c in fixture["files"]],
    )
    return fix, ""
