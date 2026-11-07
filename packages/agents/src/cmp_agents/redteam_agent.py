"""The red-team Agent's decision boundary (ticket 35, ADR-008).

The model never sees or chooses a destination. `cmp_worker.sandbox_isolation.Executor` (ticket
33) already fixes the one address a run may reach, and the model's output is a `ToolCall`: a
tool name and a path inside that same app. There is no host, URL or IP field anywhere in the
schema, so nothing the model outputs can be turned into a different destination — this is
enforced by the shape of the data, not by inspecting the model's text. `AllowList` then checks
the tool name itself against the fixed set the worker registered; it rejects anything that was
never named, and it refuses at registration time any name that reads like a shell or network
escape (reusing the same check the general `ToolRegistry` already applies).

Running a call, reading its result and feeding that result back to the model is later work in
this same ticket; this module only decides whether a call may be dispatched at all.
"""

import re
from collections.abc import Iterable
from typing import Literal

from cmp_scanners.masking import MASK, mask_text
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cmp_agents.tools import _FORBIDDEN as _UNSAFE_NAME  # single source of the banned-name rule
from cmp_agents.tools import ForbiddenToolError

OUT_OF_SCOPE_TARGET = "OUT_OF_SCOPE_TARGET"
SECRET_IN_INPUT = "SECRET_IN_INPUT"
_MIN_SECRET_LENGTH = 6
_PATH = re.compile(r"^/[^\s]*$")
# A leading digit-and-dot run (an IPv4-looking path segment) is refused too: `ToolCall.path`
# only ever reaches the one Service address the isolation session bound, so a model output that
# looks like "go to this other address" has nowhere to go even if it is not a URL.
_LOOKS_LIKE_HOST = re.compile(r"^/+(\d{1,3}\.){3}\d{1,3}(/|$)")


class ToolCall(BaseModel):
    """What the model may ask for. Nothing else reaches the executor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=2048)

    @field_validator("path")
    @classmethod
    def _a_path_inside_the_fixed_target_only(cls, value: str) -> str:
        if (
            "://" in value
            or value.startswith("//")
            or not _PATH.match(value)
            or _LOOKS_LIKE_HOST.match(value)
        ):
            raise ValueError("PATH_MUST_STAY_INSIDE_THE_TARGET")
        return value


class AllowList:
    """The fixed set of tool names a run may use. Empty unless the caller names tools, and a
    name that looks like a shell or network escape is refused when the list is built, not
    discovered later while a run is already in progress."""

    def __init__(self, names: set[str] | None = None) -> None:
        for name in names or ():
            if _UNSAFE_NAME.search(name):
                raise ForbiddenToolError(f"tool '{name}' is not an allowed kind of tool")
        self._names = frozenset(names or ())

    def check(self, call: ToolCall) -> str | None:
        """None when the call may be dispatched, otherwise the refusal code."""
        return None if call.tool in self._names else OUT_OF_SCOPE_TARGET


# --- what the model is shown (grey-box only) -----------------------------------------------------


def _redact(text: str, known_secrets: Iterable[str]) -> str:
    """Exact known values first (an app's real secret may not contain anything `mask_text`'s
    patterns would catch on their own), then the general secret-looking-string pass."""
    secrets = sorted(
        {s for s in known_secrets if len(s) >= _MIN_SECRET_LENGTH}, key=len, reverse=True
    )
    for value in secrets:
        text = text.replace(value, MASK)
    return mask_text(text)


def _still_leaking(text: str, known_secrets: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(
        len(s) >= _MIN_SECRET_LENGTH and s.casefold() in lowered for s in known_secrets
    )  # fmt: skip


class Endpoint(BaseModel):
    """One route the model may target. A path only — see `ToolCall` for why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(min_length=1, max_length=10)
    path: str = Field(min_length=1, max_length=2048)
    params: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _no_host(cls, value: str) -> str:
        return ToolCall._a_path_inside_the_fixed_target_only(value)


class AgentInput(BaseModel):
    """Everything the model is shown for one turn. No source, no `.env`, no file content, and no
    answer key: a vulnerability planted in a demo app for rehearsal is a grading tool, never
    something the model is told about."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoints: list[Endpoint]
    requirements_text: str = Field(max_length=4000)
    findings_so_far: list[str] = Field(default_factory=list, max_length=50)


def build_input(
    *,
    endpoints: list[dict],
    requirements_text: str,
    findings_so_far: list[str] | None = None,
    known_secrets: Iterable[str] = (),
) -> AgentInput:
    secrets = list(known_secrets)
    text = _redact(requirements_text, secrets)
    if _still_leaking(text, secrets):
        raise ValueError(SECRET_IN_INPUT)
    return AgentInput(
        endpoints=[Endpoint(**e) for e in endpoints],
        requirements_text=text,
        findings_so_far=findings_so_far or [],
    )


class ObservedResult(BaseModel):
    """A tool's result, wrapped so the model reads it as data, never as a new instruction.

    Masking happens once, here, before the text is shown anywhere — the same rule `Finding`
    evidence follows (ticket 34). Whatever text the target application sent back (including
    anything shaped like a command to the model) stays inside this one field, tagged as observed
    data; nothing promotes it into the instruction channel.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["OBSERVED_DATA"] = "OBSERVED_DATA"
    text: str = Field(max_length=8000)

    @classmethod
    def from_raw(cls, raw: str, *, known_secrets: Iterable[str] = ()) -> "ObservedResult":
        return cls(text=_redact(raw[:8000], known_secrets))

    def render(self) -> str:
        return f"<observed_data>\n{self.text}\n</observed_data>"


# --- the turn/time budget and the loop that enforces it ------------------------------------------

import time
from collections.abc import Callable


class TurnBudget(BaseModel):
    """How far one run may go. Reused as policy from ADR-004's cost limits; wiring this to the
    real token/dollar ledger is later work once this loop is connected to an actual LLM call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: int = Field(ge=1, le=100)
    max_seconds: float = Field(gt=0, le=3600)


class Decision(BaseModel):
    """What the model asked for this turn. `call=None` means it is done. `call` is anything a
    `ToolCall` accepts, so a caller may pass a plain dict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call: ToolCall | None = None


class LoopResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stopped_reason: Literal["DONE", "TURN_LIMIT", "TIME_LIMIT", "EXECUTOR_ERROR"]
    turns_used: int
    observations: list[ObservedResult]
    # The call behind each observation, in the same order. A refused call is here too (its
    # observation is the refusal code), so a Finding's reproduction can be checked against what
    # was actually sent and not against what a model says it sent.
    calls: list[ToolCall] = Field(default_factory=list)


def run_loop(
    decide: Callable[[list[ObservedResult]], Decision],
    dispatch: Callable[[ToolCall], ObservedResult],
    *,
    allowed: AllowList,
    budget: TurnBudget,
    clock: Callable[[], float] = time.monotonic,
) -> LoopResult:
    """Drive one run. Every observation this returns already went through `ObservedResult`'s
    masking (part 2), so nothing further needs to be redacted on the way out. A turn is spent
    whether the model's call was dispatched, refused, or the executor itself failed; only a model
    `call=None` ends the run as `DONE` instead of hitting a limit."""
    deadline = clock() + budget.max_seconds
    history: list[ObservedResult] = []
    sent: list[ToolCall] = []
    turns = 0
    while True:
        if turns >= budget.max_turns:
            return LoopResult(
                stopped_reason="TURN_LIMIT", turns_used=turns, observations=history, calls=sent
            )
        if clock() >= deadline:
            return LoopResult(
                stopped_reason="TIME_LIMIT", turns_used=turns, observations=history, calls=sent
            )
        decision = decide(history)
        turns += 1
        if decision.call is None:
            return LoopResult(
                stopped_reason="DONE", turns_used=turns, observations=history, calls=sent
            )
        refusal = allowed.check(decision.call)
        if refusal is not None:
            history.append(ObservedResult.from_raw(refusal))
            sent.append(decision.call)
            continue
        try:
            observed = dispatch(decision.call)
            history.append(observed)
            sent.append(decision.call)
        except Exception:  # noqa: BLE001 — a dead executor ends the run, it never crashes it
            return LoopResult(
                stopped_reason="EXECUTOR_ERROR", turns_used=turns, observations=history, calls=sent
            )
