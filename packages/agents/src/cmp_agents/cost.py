"""LLM spend limits (ADR-004): a price table, a durable ledger, and a governed client.

Every call reserves its worst-case cost before it is made and settles to the real cost after, so
a limit holds even when a call fails half way or an Activity is retried. Limits are per
migration, per UTC day and per UTC month, plus a kill switch. A rejected call raises
`LLMBudgetExceeded` before anything is sent to the provider.
"""

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cmp_agents.llm import LLMClient, LLMError, LLMResponse, Message, ToolDefinition

_MICRO = 1_000_000  # money is kept in micro-dollars so sums never drift


class LLMBudgetExceeded(LLMError):
    """Raised before a call when it could push a limit over. `code` says which one."""


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


# Anthropic first-party rates, checked 2026-11-05 (claude-sonnet-5 and claude-opus-5-5 added
# 2026-11-08). Update when the price list changes.
PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50),
    "claude-sonnet-5": Price(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50),
    # cache_write is not separately confirmed for this model; carried over at the same ratio
    # (1.25x input) every other current-generation price in this table uses.
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.20, cache_write=5.00),
}


@dataclass(frozen=True)
class Limits:
    per_run_usd: float = 3.0
    per_day_usd: float = 20.0
    per_month_usd: float = 100.0
    warn_fraction: float = 0.8


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def cost_micro(price: Price, usage: Usage) -> int:
    return round(
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    )  # tokens x $/MTok = micro-dollars


class CostLedger:
    """File-backed totals with an exclusive lock, so every worker process shares one budget."""

    def __init__(
        self,
        path: Path | str,
        limits: Limits = Limits(),  # noqa: B008
        *,
        kill_switch: Path | str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = Path(path)
        self.limits = limits
        self.kill_switch = Path(kill_switch) if kill_switch else None
        self._clock = clock

    # ------------------------------------------------------------------ storage

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"runs": {}, "days": {}, "months": {}}

    def _write(self, data: dict[str, Any]) -> None:
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(data, stream, sort_keys=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    # --------------------------------------------------------------------- state

    def _keys(self) -> tuple[str, str]:
        now = self._clock()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def disabled(self) -> bool:
        return (self.kill_switch is not None and self.kill_switch.exists()) or os.environ.get(
            "CMP_LLM_DISABLED", ""
        ).lower() in {"1", "true", "yes"}

    def spent(self, run: str) -> dict[str, float]:
        with self._locked():
            data = self._read()
        day, month = self._keys()
        return {
            "run": data["runs"].get(run, 0) / _MICRO,
            "day": data["days"].get(day, 0) / _MICRO,
            "month": data["months"].get(month, 0) / _MICRO,
        }

    # ------------------------------------------------------------- reserve/settle

    def reserve(self, run: str, worst_case_micro: int) -> int:
        """Add the worst-case cost now. Raises before adding when a limit would be passed."""
        if self.disabled():
            raise LLMBudgetExceeded("LLM_DISABLED")
        day, month = self._keys()
        with self._locked():
            data = self._read()
            checks = (
                ("BUDGET_RUN", data["runs"].get(run, 0), self.limits.per_run_usd),
                ("BUDGET_DAY", data["days"].get(day, 0), self.limits.per_day_usd),
                ("BUDGET_MONTH", data["months"].get(month, 0), self.limits.per_month_usd),
            )
            for code, current, limit in checks:
                if current + worst_case_micro > round(limit * _MICRO):
                    raise LLMBudgetExceeded(code)
            data["runs"][run] = data["runs"].get(run, 0) + worst_case_micro
            data["days"][day] = data["days"].get(day, 0) + worst_case_micro
            data["months"][month] = data["months"].get(month, 0) + worst_case_micro
            self._write(data)
        return worst_case_micro

    def settle(self, run: str, reserved_micro: int, actual_micro: int) -> None:
        """Replace the reservation with what the call really cost (never below zero)."""
        delta = actual_micro - reserved_micro
        day, month = self._keys()
        with self._locked():
            data = self._read()
            for bucket, key in (("runs", run), ("days", day), ("months", month)):
                data[bucket][key] = max(0, data[bucket].get(key, 0) + delta)
            self._write(data)

    def warnings(self, run: str) -> list[str]:
        spent, fraction = self.spent(run), self.limits.warn_fraction
        caps = {
            "run": self.limits.per_run_usd,
            "day": self.limits.per_day_usd,
            "month": self.limits.per_month_usd,
        }
        return [f"{k}:{spent[k]:.2f}/{caps[k]:.2f}" for k in caps if spent[k] >= fraction * caps[k]]


def estimate_input_tokens(
    system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
) -> int:
    """Deliberately high estimate (one token per 2.5 characters) so reservations err safe."""
    size = len(system)
    for message in messages:
        size += len(message.content) + sum(
            len(json.dumps(c.arguments, default=str)) + len(c.name) for c in message.tool_calls
        )
    size += sum(len(t.name) + len(t.description) + len(json.dumps(t.parameters)) for t in tools)
    return int(size / 2.5) + 64


class GovernedLLMClient:
    """Wraps any `LLMClient` with the ledger. `max_output_tokens` bounds every call."""

    def __init__(
        self,
        inner: LLMClient,
        ledger: CostLedger,
        *,
        run_id: str,
        model: str,
        max_output_tokens: int,
        price: Price | None = None,
    ) -> None:
        if price is None and model not in PRICES:
            raise ValueError(f"no price for model '{model}'")
        self.inner, self.ledger, self.run_id = inner, ledger, run_id
        self.price = price or PRICES[model]
        self.max_output_tokens = max_output_tokens

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse:
        worst = cost_micro(
            self.price,
            Usage(
                input_tokens=estimate_input_tokens(system, messages, tools),
                output_tokens=self.max_output_tokens,
            ),
        )
        reserved = self.ledger.reserve(self.run_id, worst)
        # A failed call may still have been billed, so its reservation is kept, not refunded.
        response = self.inner.complete(system=system, messages=messages, tools=tools)
        usage = Usage(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_write_tokens=response.cache_write_tokens,
        )
        self.ledger.settle(self.run_id, reserved, cost_micro(self.price, usage))
        return response
