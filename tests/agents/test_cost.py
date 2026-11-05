import threading
from datetime import UTC, datetime

import pytest
from cmp_agents.cost import (
    PRICES,
    CostLedger,
    GovernedLLMClient,
    Limits,
    LLMBudgetExceeded,
    Price,
    Usage,
    cost_micro,
    estimate_input_tokens,
)
from cmp_agents.llm import FakeLLMClient, LLMResponse, Message

PRICE = Price(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)
MESSAGES = [Message(role="user", content="hello")]


def ledger(tmp_path, **kw):
    limits = kw.pop("limits", Limits(per_run_usd=1.0, per_day_usd=2.0, per_month_usd=5.0))
    return CostLedger(tmp_path / "ledger.json", limits, **kw)


def governed(tmp_path, responses, *, run="mig-1", max_output=1000, **kw):
    inner = FakeLLMClient(responses)
    book = kw.pop("ledger", None) or ledger(tmp_path)
    return (
        GovernedLLMClient(
            inner,
            book,
            run_id=run,
            model="claude-sonnet-5-5",
            max_output_tokens=max_output,
            price=PRICE,
        ),
        inner,
        book,
    )


def say(**kw):
    return LLMResponse(text="ok", **kw)


def test_the_sonnet_5_5_price_is_in_the_table():
    assert PRICES["claude-sonnet-5-5"].input == 2.0 and PRICES["claude-sonnet-5-5"].output == 10.0
    assert PRICES["claude-sonnet-5-5"].cache_read == 0.20


def test_cost_adds_input_output_and_cache_tokens_in_micro_dollars():
    usage = Usage(input_tokens=100_000, output_tokens=10_000, cache_read_tokens=1_000_000)
    assert cost_micro(PRICE, usage) == 200_000 + 100_000 + 200_000  # $0.50


def test_a_call_is_settled_to_its_real_cost(tmp_path):
    client, _, book = governed(tmp_path, [say(input_tokens=1000, output_tokens=100)])
    client.complete(system="s", messages=MESSAGES, tools=[])
    assert book.spent("mig-1")["run"] == pytest.approx(0.001 * 2 + 0.0001 * 10)  # $0.003


def test_the_worst_case_is_reserved_so_a_call_that_could_pass_the_limit_is_refused_unsent(tmp_path):
    client, inner, book = governed(
        tmp_path, [say()], max_output=200_000
    )  # worst case $2 > $1 per run
    with pytest.raises(LLMBudgetExceeded) as exc:
        client.complete(system="s", messages=MESSAGES, tools=[])
    assert exc.value.code == "BUDGET_RUN" and inner.calls == []
    assert book.spent("mig-1")["run"] == 0


def test_the_per_run_limit_is_shared_by_every_client_of_the_same_run(tmp_path):
    book = ledger(tmp_path, limits=Limits(per_run_usd=0.05, per_day_usd=9, per_month_usd=9))
    a, _, _ = governed(tmp_path, [say(output_tokens=4000)] * 3, ledger=book, max_output=4000)
    b, _, _ = governed(tmp_path, [say(output_tokens=4000)] * 3, ledger=book, max_output=4000)
    a.complete(system="s", messages=MESSAGES, tools=[])  # $0.04 spent
    with pytest.raises(LLMBudgetExceeded) as exc:
        b.complete(system="s", messages=MESSAGES, tools=[])  # another $0.04 worst case
    assert exc.value.code == "BUDGET_RUN"
    other, _, _ = governed(tmp_path, [say()], ledger=book, run="mig-2", max_output=10)
    other.complete(
        system="s", messages=MESSAGES, tools=[]
    )  # a different run still has its own limit


def test_day_and_month_limits_apply_across_runs_and_reset_with_the_calendar(tmp_path):
    clock = {"now": datetime(2026, 11, 5, 10, tzinfo=UTC)}
    book = ledger(
        tmp_path,
        limits=Limits(per_run_usd=9, per_day_usd=0.05, per_month_usd=0.09),
        clock=lambda: clock["now"],
    )
    one, _, _ = governed(tmp_path, [say(output_tokens=4000)], ledger=book, run="a", max_output=4000)
    one.complete(system="s", messages=MESSAGES, tools=[])
    two, _, _ = governed(tmp_path, [say(output_tokens=4000)], ledger=book, run="b", max_output=4000)
    with pytest.raises(LLMBudgetExceeded) as day:
        two.complete(system="s", messages=MESSAGES, tools=[])
    assert day.value.code == "BUDGET_DAY"
    clock["now"] = datetime(
        2026, 11, 6, 10, tzinfo=UTC
    )  # next day: day limit is fresh, month is not
    two.complete(system="s", messages=MESSAGES, tools=[])
    clock["now"] = datetime(2026, 11, 7, 10, tzinfo=UTC)
    three, _, _ = governed(tmp_path, [say()], ledger=book, run="c", max_output=4000)
    with pytest.raises(LLMBudgetExceeded) as month:
        three.complete(system="s", messages=MESSAGES, tools=[])
    assert month.value.code == "BUDGET_MONTH"
    clock["now"] = datetime(2026, 12, 1, 10, tzinfo=UTC)
    three.complete(system="s", messages=MESSAGES, tools=[])


def test_the_kill_switch_file_and_the_environment_variable_stop_every_call(tmp_path, monkeypatch):
    switch = tmp_path / "STOP"
    client, inner, _ = governed(tmp_path, [say()] * 3, ledger=ledger(tmp_path, kill_switch=switch))
    client.complete(system="s", messages=MESSAGES, tools=[])
    switch.write_text("")
    with pytest.raises(LLMBudgetExceeded) as exc:
        client.complete(system="s", messages=MESSAGES, tools=[])
    assert exc.value.code == "LLM_DISABLED" and len(inner.calls) == 1
    switch.unlink()
    monkeypatch.setenv("CMP_LLM_DISABLED", "1")
    with pytest.raises(LLMBudgetExceeded):
        client.complete(system="s", messages=MESSAGES, tools=[])


def test_a_failed_call_keeps_its_reservation(tmp_path):
    client, _, book = governed(tmp_path, [])  # the fake raises "script exhausted"
    with pytest.raises(RuntimeError):
        client.complete(system="s", messages=MESSAGES, tools=[])
    assert book.spent("mig-1")["run"] > 0


def test_warnings_appear_at_eighty_percent(tmp_path):
    book = ledger(tmp_path, limits=Limits(per_run_usd=0.01, per_day_usd=9, per_month_usd=9))
    client, _, _ = governed(tmp_path, [say(output_tokens=900)], ledger=book, max_output=900)
    client.complete(system="s", messages=MESSAGES, tools=[])
    assert book.warnings("mig-1") == ["run:0.01/0.01"]


def test_concurrent_calls_never_pass_the_limit(tmp_path):
    book = ledger(tmp_path, limits=Limits(per_run_usd=0.10, per_day_usd=9, per_month_usd=9))
    accepted, refused = [], []

    def go():
        client, _, _ = governed(tmp_path, [say(output_tokens=3000)], ledger=book, max_output=3000)
        try:
            client.complete(system="s", messages=MESSAGES, tools=[])
            accepted.append(1)
        except LLMBudgetExceeded:
            refused.append(1)

    threads = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert book.spent("mig-1")["run"] <= 0.10 + 1e-9
    assert len(accepted) == 3 and len(refused) == 9  # $0.03 each


def test_the_ledger_file_is_private_and_holds_only_totals(tmp_path):
    client, _, book = governed(tmp_path, [say(input_tokens=10, output_tokens=1)])
    client.complete(system="a secret-looking prompt", messages=MESSAGES, tools=[])
    text = book.path.read_text()
    assert "secret" not in text and "hello" not in text
    assert oct(book.path.stat().st_mode & 0o777) == "0o600"


def test_the_input_estimate_leans_high():
    assert estimate_input_tokens("x" * 2500, MESSAGES, []) >= 1000


def test_an_unknown_model_without_a_price_is_refused(tmp_path):
    with pytest.raises(ValueError):
        GovernedLLMClient(
            FakeLLMClient([]), ledger(tmp_path), run_id="r", model="mystery", max_output_tokens=10
        )


# ------------------------------------------------- what the agents do when a call is refused


def test_an_agent_run_stops_with_the_budget_code_and_the_loops_do_not_retry(tmp_path):
    from cmp_agents.fixer import FixInput, propose_fix
    from cmp_agents.runtime import Budget, run_agent
    from cmp_agents.tester import SuiteRequest, generate_tests
    from cmp_agents.tools import ToolRegistry
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int

    switch = tmp_path / "STOP"
    switch.write_text("")
    book = ledger(tmp_path, kill_switch=switch)
    client, inner, _ = governed(tmp_path, [say()], ledger=book)

    run = run_agent(
        role="t", task="x", llm=client, tools=ToolRegistry(), submit_name="submit",
        result_model=Out, budget=Budget(),
    )  # fmt: skip
    assert (run.status, run.failure_reason, run.fatal) == ("FAILED", "LLM_DISABLED", True)

    generated = generate_tests(SuiteRequest(usage_summary="u"), llm=client, max_attempts=3)
    assert generated.status == "FAILED" and generated.failure_reason == "LLM_DISABLED"
    fixed = propose_fix(FixInput(failure_class="SPEC", reasons=[]), llm=client, max_attempts=3)
    assert fixed.status == "FAILED" and fixed.reason == "LLM_DISABLED"
    assert inner.calls == []  # nothing was ever sent


def test_the_mapper_gives_up_after_one_refused_call(tmp_path):
    from pathlib import Path

    from cmp_agents.catalog import load_catalog
    from cmp_agents.mapper import MapperInput, map_usage

    root = Path(__file__).parent.parent.parent
    catalog = load_catalog(root / "catalog", skills_root=root / "skills")
    switch = tmp_path / "STOP"
    switch.write_text("")
    client, inner, _ = governed(tmp_path, [say()], ledger=ledger(tmp_path, kill_switch=switch))
    result = map_usage(
        MapperInput(evidence_summary="ENV_READ DATABASE_URL"), llm=client, catalog=catalog,
        skills_root=root / "skills", max_attempts=3,
    )  # fmt: skip
    assert result.status == "NEEDS_REVIEW" and result.failure_reason == "LLM_DISABLED"
    assert result.attempts == 1 and inner.calls == []
