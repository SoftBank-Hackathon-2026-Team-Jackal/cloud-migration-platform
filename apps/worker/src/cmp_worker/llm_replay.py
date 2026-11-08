"""Replay a model's earlier answer for the same inputs, so a repeat run skips the model (ticket 39).

The Fixer and the Tester are the slow, model-bound steps of a Sandbox run. For the same source (its
tree hash), the same requirements and the same failure class they are asked the same question, so a
stored answer is handed back instead of calling the model again. Only the model call is skipped:
everything after it (the security-lowering checks, applying the fix, validating the tests) runs
every time on whatever the answer is, so a replayed answer gets no shortcut past a safety check.

Best effort throughout: no store, an unreachable store or an unreadable object is a miss and the
model is asked as before; a store that cannot be written never fails the step.
"""

import hashlib
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from cmp_agents.fixer import CodeFix, FixOutcome, RequirementsFix
from cmp_agents.tester import GenerateResult
from cmp_testsuite.model import SuiteCase
from cmp_testsuite.record import Probe

from cmp_worker.redteam_cache import RedTeamCacheError, RedTeamCacheStore

# The instance role may read and write `redteam/*` in the deploy bucket only (build-stack.yaml,
# RedteamCacheObjects), so these keys live under that prefix. Moving them needs an IAM change.
_PREFIX = "redteam/replay"


def replay_key(role: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return f"{_PREFIX}/{role}/{digest}.json"


def replayed(
    cache: RedTeamCacheStore | None,
    key: str | None,
    produce: Callable[[], Any],
    *,
    dump: Callable[[Any], dict | None],
    load: Callable[[dict], Any],
) -> Any:
    """The stored answer for `key`, else `produce()` stored afterwards.

    `dump` turns a fresh answer into JSON, or None when it is not worth keeping (a failure must be
    asked again next time). `load` turns stored JSON back into the answer; one that cannot be
    loaded is a miss.
    """
    if cache is None or key is None:
        return produce()
    try:
        stored = cache.get(key)
    except RedTeamCacheError:
        stored = None
    if stored is not None:
        # An object that no longer fits the current models is a miss, not a crash.
        with suppress(Exception):
            return load(stored)
    answer = produce()
    data = dump(answer)
    if data is not None:
        try:
            cache.put(key, data)
        except RedTeamCacheError:
            pass
    return answer


# --- the two answers worth replaying -------------------------------------------------------------


def dump_fix(outcome: FixOutcome) -> dict | None:
    """A proposed fix only. A refusal or a failed call is asked again next time."""
    if outcome.status != "PROPOSED":
        return None
    return {
        "requirements_fix": outcome.requirements_fix.model_dump(mode="json")
        if outcome.requirements_fix
        else None,
        "code_fix": outcome.code_fix.model_dump(mode="json") if outcome.code_fix else None,
        "reason": outcome.reason,
    }


def load_fix(data: dict) -> FixOutcome:
    return FixOutcome(
        status="PROPOSED",
        requirements_fix=RequirementsFix.model_validate(data["requirements_fix"])
        if data.get("requirements_fix")
        else None,
        code_fix=CodeFix.model_validate(data["code_fix"]) if data.get("code_fix") else None,
        reason=data.get("reason"),
    )


def dump_tests(result: GenerateResult) -> dict | None:
    if result.status != "GENERATED":
        return None
    return {
        "cases": [c.model_dump(mode="json") for c in result.cases],
        "probes": [p.model_dump(mode="json") for p in result.probes],
    }


def load_tests(data: dict) -> GenerateResult:
    return GenerateResult(
        status="GENERATED",
        cases=[SuiteCase.model_validate(c) for c in data["cases"]],
        failure_reason=None,
        record=None,
        probes=tuple(Probe.model_validate(p) for p in data["probes"]),
    )
