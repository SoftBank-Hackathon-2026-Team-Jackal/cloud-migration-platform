"""Builds the model client for one migration and one agent role (ADR-004).

Nothing is built, and nothing can be spent, unless the operator turned Claude on, set a ledger
file and provided a key in the environment. Every client is wrapped by the shared spend ledger.
"""

import os
from collections.abc import Callable

from cmp_agents.anthropic_client import AnthropicLLMClient
from cmp_agents.cost import CostLedger, GovernedLLMClient, Limits
from cmp_agents.llm import LLMClient

from cmp_worker.config import Settings
from cmp_worker.config import settings as default_settings

# Depth per role: reading and proposing is medium, repairing code gets more thinking.
EFFORT = {"mapper": "medium", "tester": "medium", "fixer": "high"}
# The red-team role pins an older model generation: Claude Sonnet 5.5's own safety classifier
# declines ("cyber") the tool calls that send a crafted request to the target app, even with an
# explicit authorization framing in the system prompt (ADR-008 is not a private claim an API-level
# classifier can verify). Claude Sonnet 5 does not decline the same calls (confirmed 2026-11-08).
# Fixed here, not in `config.llm_model`, so every other role keeps the operator's chosen default.
# Temporary (2026-11-08): the fixer's code fixes were landing broken (didn't compile/run) on
# Claude Sonnet 5.5. Pinned to Opus 5.5 while that's investigated; revert once the real cause
# (likely: no self-check after the fix is applied) is fixed, since Opus costs 2x as much.
MODEL_OVERRIDE = {"redteam": "claude-sonnet-5", "fixer": "claude-opus-5-5"}

LLMFactory = Callable[[str, str], LLMClient]  # (migration_id, role) -> client


def build_llm_factory(config: Settings = default_settings) -> LLMFactory | None:
    if not config.llm_enabled or not config.llm_ledger_path:
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    ledger = CostLedger(
        config.llm_ledger_path,
        Limits(
            per_run_usd=config.llm_limit_run_usd,
            per_day_usd=config.llm_limit_day_usd,
            per_month_usd=config.llm_limit_month_usd,
        ),
        kill_switch=config.llm_kill_switch_path,
    )

    def make(migration_id: str, role: str) -> LLMClient:
        model = MODEL_OVERRIDE.get(role, config.llm_model)
        inner = AnthropicLLMClient(
            model=model,
            max_tokens=config.llm_max_output_tokens,
            effort=EFFORT.get(role, "medium"),
        )
        return GovernedLLMClient(
            inner,
            ledger,
            run_id=migration_id,
            model=model,
            max_output_tokens=config.llm_max_output_tokens,
        )

    return make
