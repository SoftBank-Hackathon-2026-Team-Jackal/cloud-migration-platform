"""Ticket 35, part 3: the turn/time budget and the loop that enforces it.

The model is asked for a decision, a turn at a time, through `decide(history) -> Decision`.
`run_loop` stops on its own once a limit is hit and always returns what was found so far; it
never lets a turn start once the budget is spent, and a call outside the allow list is refused
without ever reaching the executor.
"""

from cmp_agents.redteam_agent import (
    AllowList,
    Decision,
    ObservedResult,
    TurnBudget,
    run_loop,
)


def test_the_loop_stops_by_itself_when_the_model_says_it_is_done():
    decide = lambda history: Decision(call=None)
    result = run_loop(
        decide, dispatch=lambda call: (_ for _ in ()).throw(AssertionError("never called")),
        allowed=AllowList({"probe_get"}), budget=TurnBudget(max_turns=10, max_seconds=60),
    )  # fmt: skip
    assert result.stopped_reason == "DONE" and result.turns_used == 1


def test_the_loop_stops_at_the_turn_limit_and_keeps_what_was_found(monkeypatch):
    calls = []

    def decide(history):
        calls.append(len(history))
        return Decision(call={"tool": "probe_get", "path": f"/x/{len(history)}"})

    def dispatch(call):
        return ObservedResult.from_raw(f"seen {call.path}")

    result = run_loop(
        decide, dispatch, allowed=AllowList({"probe_get"}), budget=TurnBudget(max_turns=3, max_seconds=60)
    )  # fmt: skip
    assert result.stopped_reason == "TURN_LIMIT" and result.turns_used == 3
    assert len(calls) == 3 and calls == [0, 1, 2]  # history grows by one observation per turn


def test_a_turn_never_starts_once_the_clock_runs_out():
    clock = iter([0.0, 0.0, 61.0])  # the third read is past the deadline
    decide = lambda history: Decision(call={"tool": "probe_get", "path": "/x"})
    dispatch = lambda call: ObservedResult.from_raw("ok")
    result = run_loop(
        decide, dispatch, allowed=AllowList({"probe_get"}),
        budget=TurnBudget(max_turns=100, max_seconds=60), clock=lambda: next(clock),
    )  # fmt: skip
    assert result.stopped_reason == "TIME_LIMIT" and result.turns_used == 1


def test_a_call_outside_the_allow_list_is_refused_and_never_dispatched():
    decide = lambda history: Decision(call={"tool": "run_shell_like", "path": "/x"})
    dispatch = lambda call: (_ for _ in ()).throw(AssertionError("must not be dispatched"))
    result = run_loop(
        decide, dispatch, allowed=AllowList(), budget=TurnBudget(max_turns=1, max_seconds=60)
    )  # fmt: skip
    assert result.stopped_reason == "TURN_LIMIT"  # the one allowed turn was spent on a refusal
    assert result.observations[-1].text == "OUT_OF_SCOPE_TARGET"


def test_everything_found_so_far_is_returned_even_if_dispatch_itself_breaks():
    calls = {"n": 0}

    def decide(history):
        return Decision(call={"tool": "probe_get", "path": "/x"})

    def dispatch(call):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the executor Pod died")
        return ObservedResult.from_raw(f"ok {calls['n']}")

    result = run_loop(
        decide, dispatch, allowed=AllowList({"probe_get"}),
        budget=TurnBudget(max_turns=5, max_seconds=60),
    )  # fmt: skip
    assert result.stopped_reason == "EXECUTOR_ERROR" and result.turns_used == 2
    assert [o.text for o in result.observations] == ["ok 1"]


def test_the_budget_itself_is_bounded():
    import pytest
    from pydantic import ValidationError

    for turns, seconds in [(0, 60), (101, 60), (5, 0), (5, 3601)]:
        with pytest.raises(ValidationError):
            TurnBudget(max_turns=turns, max_seconds=seconds)
