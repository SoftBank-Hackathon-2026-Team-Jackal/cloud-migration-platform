"""The red-team step as one Activity-sized unit (ticket 37, ADR-008).

Tickets 33-36 each built a piece and none of them called the others. This module is the place they
meet, in the order the ADR fixes: `RedTeamRunner` proves isolation, cuts the outside world and
resets data (ticket 33/34), the Agent loop picks tool calls inside the one target (ticket 35), and
what it finds is turned into Findings with masked evidence (ticket 34) or, on a later Digest,
re-checked one Finding at a time (ticket 36).

What is *not* here: the model call, the tool runner inside the executor Pod, and the judgement that
turns an observation into a Finding. They arrive through `RedTeamKit` so that this connection can be
tested with fakes and so that nothing here can spend model money or reach a cluster by accident. A
worker built without a kit reports `REDTEAM_NOT_CONFIGURED` and attacks nothing.
"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from cmp_agents.redteam_agent import (
    AgentInput,
    AllowList,
    Decision,
    Endpoint,
    LoopResult,
    ObservedResult,
    ToolCall,
    TurnBudget,
    build_input,
    run_loop,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cmp_worker.artifact_store import ArtifactStore
from cmp_worker.redteam_cache import RedTeamCacheError, RedTeamCacheStore, cache_key
from cmp_worker.redteam_fixloop import (
    MAX_SECURITY_FIX_ATTEMPTS,
    RevalidationLedger,
    revalidate,
)
from cmp_worker.redteam_safeguards import (
    Approval,
    Finding,
    Intensity,
    RedTeamRunner,
    RunConfig,
    approval_request,
    make_finding,
    store_evidence,
    summarize,
)
from cmp_worker.sandbox_deploy import SandboxError, SandboxPlan
from cmp_worker.sandbox_isolation import Executor

NOT_CONFIGURED = "REDTEAM_NOT_CONFIGURED"
NOT_APPROVED = "REDTEAM_NOT_APPROVED"
SECRET_IN_EVIDENCE = "SECRET_IN_EVIDENCE"
MAX_SECURITY_ATTEMPTS = MAX_SECURITY_FIX_ATTEMPTS
"""R4 (ADR-008): the Workflow gives security fixes this many rounds, apart from functional ones."""


class RedTeamInput(BaseModel):
    """What the Workflow argument says about a red-team run. Names and numbers only: there is no
    host, URL or address field, because the target is the one Service the isolation session
    binds (ADR-008 decision 5)."""

    model_config = ConfigDict(extra="forbid")

    intensity: Intensity
    time_budget_seconds: int = Field(ge=30, le=3600)
    target_resource_id: str = Field(min_length=1)
    consent_ref: str = Field(min_length=1)  # the same consent `SandboxSettings.redteam` needs
    endpoints: list[Endpoint]  # grey-box input (R7): method, path, parameter names
    max_turns: int = Field(default=30, ge=1, le=100)
    # Ticket 39 draft: redeploying the exact same source replays the last real attack's result
    # instead of spending another one. On by default; a toggle the settings screen can turn off
    # per run (e.g. a rehearsal that wants a fresh, uncached pass).
    cache_enabled: bool = True
    # Demo mock (`SandboxSettings.redteam_mock`): replay a recorded real run, attack nothing.
    mock: bool = False


class Brain(Protocol):
    """The model's part (see `cmp_worker.redteam_brain.Brain`)."""

    error: str | None

    def decide(self, agent_input: AgentInput) -> Callable[[list[ObservedResult]], Decision]: ...

    def detect(self, agent_input: AgentInput, loop: LoopResult) -> list[dict[str, Any]]: ...

    def reproduces(self, finding: Finding, observed: ObservedResult) -> bool: ...


@dataclass(frozen=True)
class RedTeamKit:
    """Everything a run needs that is not decided by the safeguards themselves.

    `brain(migration_id)` gives one run's model calls (the spend is booked against that
    migration); `dispatch(executor, secrets)` returns the function that runs one allowed tool in
    that executor, masking the app's own secret values from what it returns. `detect` drafts are
    dicts with `category`, `severity`, `endpoint`, `summary`, `repro_summary`, `evidence` and
    `repro: {tool, path}`. `known_secrets` are the values the Sandbox Secret holds.
    """

    runner: RedTeamRunner
    store: ArtifactStore
    allowed: AllowList
    brain: Callable[[str], Brain]
    dispatch: Callable[[Executor, list[str]], Callable[[ToolCall], ObservedResult]]
    known_secrets: Callable[[SandboxPlan], list[str]] = lambda plan: []
    # Ticket 39 draft: optional. None (the default -- no bucket configured) makes every cache
    # lookup a clean, silent miss; this never gates whether a run may start.
    cache: RedTeamCacheStore | None = None


def request_for(plan: SandboxPlan, red: RedTeamInput, *, now: float | None = None) -> dict:
    """What the approver is shown, and how long a yes may be waited for before the Sandbox's own
    life makes the run impossible."""
    from cmp_worker.redteam_safeguards import SAFETY_MARGIN_SECONDS

    config = RunConfig(intensity=red.intensity, time_budget_seconds=red.time_budget_seconds)
    moment = time.time() if now is None else now
    wait = plan.expires_at - moment - red.time_budget_seconds - SAFETY_MARGIN_SECONDS
    return {"request": approval_request(plan, config), "wait_seconds": max(0, int(wait))}


def _skipped(code: str, previous: list[dict]) -> dict:
    return {
        "status": "SKIPPED", "code": code, "open": previous, "resolved": [], "withheld": [],
        "summary": summarize([]).model_dump(mode="json"), "stopped_reason": None,
    }  # fmt: skip


def _finding_id(category: str, endpoint: str, repro: dict) -> str:
    """The same weakness on the same route keeps one id from one image to the next, so a re-check
    can tell whether *that* Finding is gone."""
    raw = f"{category}|{endpoint}|{repro.get('tool')}|{repro.get('path')}"
    return f"{category.lower()[:24]}-{hashlib.sha256(raw.encode()).hexdigest()[:10]}"


def _text(requirements_text: str, limit: int = 4000) -> str:
    return requirements_text[:limit]


def run_stage(
    kit: RedTeamKit | None,
    *,
    plan: SandboxPlan,
    red: RedTeamInput,
    approval: dict | None,
    digest: str,
    suite_sha256: str,
    requirements_text: str,
    previous: list[dict],
    migration_id: str = "",
    source_sha256: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    """One approved run. First time (`previous` empty) the Agent explores; with `previous` it only
    replays those Findings' own reproductions against `digest` and nothing new is tried.

    Never raises for a refused or failed run: the reason is returned, because the Sandbox run this
    belongs to has already passed and a refusal here must not undo that (R3: shown, not a gate).

    `source_sha256` (ticket 39 draft) is the build step's own hash of the source -- never
    `digest`, which is not stable across rebuilds of identical source (the Go toolchain embeds a
    build ID). When given, `red.cache_enabled` and `kit.cache` are set, a result already recorded
    for this exact source and this exact set of Findings is replayed with no attack dispatched and
    no approval spent; a fresh result is cached the same way afterward.
    """
    if kit is None:
        return _skipped(NOT_CONFIGURED, previous)
    try:
        approved = Approval.model_validate(approval) if approval else None
    except ValidationError:
        return _skipped(NOT_APPROVED, previous)
    # `cache_enabled` only decides whether the stored result is *read* back. A real run is always
    # written (overwriting whatever the same key held), so the object under a key is the latest
    # result and a later run with the cache on reads that latest one.
    cache_entry_key = None
    if source_sha256 and kit.cache is not None:
        cache_entry_key = cache_key(source_sha256, (f["finding_id"] for f in previous))
        if red.cache_enabled:
            try:
                cached = kit.cache.get(cache_entry_key)
            except RedTeamCacheError:
                cached = None  # an unreachable cache is a miss, never a reason to fail the run
            if cached is not None:
                return cached
    config = RunConfig(intensity=red.intensity, time_budget_seconds=red.time_budget_seconds)
    secrets = kit.known_secrets(plan)
    brain = kit.brain(migration_id)
    found: list[Finding] = []
    repros: dict[str, dict] = {}
    withheld: list[dict] = []
    resolved: list[str] = []
    stopped: str | None = None
    code: str | None = None
    ran = False
    try:
        with kit.runner.run(
            plan, target_resource_id=red.target_resource_id, config=config, approval=approved
        ) as run:
            dispatch = kit.dispatch(run.executor, secrets)
            if previous:
                found, resolved, stopped = _recheck(brain, dispatch, previous, digest, clock)
                repros = {f["finding_id"]: f["repro"] for f in previous}
            else:
                agent_input = build_input(
                    endpoints=[e.model_dump() for e in red.endpoints],
                    requirements_text=_text(requirements_text),
                    known_secrets=secrets,
                )
                loop = run_loop(
                    brain.decide(agent_input),
                    dispatch,
                    allowed=kit.allowed,
                    budget=TurnBudget(
                        max_turns=red.max_turns,
                        max_seconds=max(1.0, min(run.deadline - time.time(), 3600.0)),
                    ),
                )
                stopped = loop.stopped_reason
                found, repros, withheld = _findings(
                    kit, brain.detect(agent_input, loop), digest, suite_sha256, red.intensity,
                    secrets, clock,
                )  # fmt: skip
            ran = True
    except SandboxError as exc:
        # A start that was refused (no approval, isolation not proven, no time) attacked nothing;
        # a failure on the way out (data not put back) happened after the work and is reported
        # with what the work found.
        if not ran:
            return _skipped(exc.code, previous)
        code = exc.code
    status = "ERROR" if code else "DONE"
    result = {
        "status": status, "code": code,
        "open": [{**f.model_dump(mode="json"), "repro": repros[f.finding_id]} for f in found],
        "resolved": resolved, "withheld": withheld, "model_error": brain.error,
        "summary": summarize(found).model_dump(mode="json"), "stopped_reason": stopped,
    }  # fmt: skip
    # Only a clean, real DONE is worth replaying later -- a partial failure (ERROR) must be
    # retried for real next time, not handed back from the cache.
    if cache_entry_key is not None and status == "DONE":
        try:
            kit.cache.put(cache_entry_key, result)
        except RedTeamCacheError:
            pass  # best-effort: a cache write failing must not fail a run that already succeeded
    return result


def _findings(
    kit: RedTeamKit,
    drafts: list[dict[str, Any]],
    digest: str,
    suite_sha256: str,
    intensity: Intensity,
    secrets: list[str],
    clock: Callable[[], datetime],
) -> tuple[list[Finding], dict[str, dict], list[dict]]:
    found: dict[str, Finding] = {}
    repros: dict[str, dict] = {}
    withheld: list[dict] = []
    for draft in drafts:
        repro = ToolCall.model_validate(draft["repro"]).model_dump()  # stays inside the target
        fid = _finding_id(draft["category"], draft["endpoint"], repro)
        if fid in found:
            continue
        stored = store_evidence(
            kit.store, finding_id=fid, evidence=draft.get("evidence", ""), known_secrets=secrets
        )
        if stored.status == SECRET_IN_EVIDENCE:
            withheld.append({"finding_id": fid, "code": SECRET_IN_EVIDENCE})
            continue  # a Finding whose evidence still holds a secret is not kept (ADR-008 6)
        found[fid] = make_finding(
            known_secrets=secrets, finding_id=fid, category=draft["category"],
            severity=draft["severity"], endpoint=draft["endpoint"], summary=draft["summary"],
            repro_summary=draft["repro_summary"], evidence_ref=stored.ref, image_digest=digest,
            suite_sha256=suite_sha256, intensity=intensity, executed_at=clock(),
        )  # fmt: skip
        repros[fid] = repro
    return list(found.values()), repros, withheld


def _recheck(
    brain: Brain,
    dispatch: Callable[[ToolCall], ObservedResult],
    previous: list[dict],
    digest: str,
    clock: Callable[[], datetime],
) -> tuple[list[Finding], list[str], str | None]:
    """Replay each earlier Finding's own request on the new image. A result is only ever recorded
    against `digest` (P-10); an executor that fails leaves the Finding open, never fixed."""
    ledger = RevalidationLedger()
    still_open: list[Finding] = []
    resolved: list[str] = []
    stopped: str | None = None
    for entry in previous:
        finding = Finding.model_validate({k: v for k, v in entry.items() if k != "repro"})
        call = ToolCall.model_validate(entry["repro"])

        def reproduce(f: Finding, _digest: str, call: ToolCall = call) -> bool:
            return brain.reproduces(f, dispatch(call))

        try:
            record = revalidate(finding, digest, reproduce=reproduce, ledger=ledger)
        except Exception:  # noqa: BLE001 - a dead executor is "could not tell", not "fixed"
            stopped = "EXECUTOR_ERROR"
            still_open.append(finding)
            continue
        if record.status == "FIXED":
            resolved.append(finding.finding_id)
        else:
            still_open.append(
                finding.model_copy(update={"image_digest": digest, "executed_at": clock()})
            )
    return still_open, resolved, stopped
