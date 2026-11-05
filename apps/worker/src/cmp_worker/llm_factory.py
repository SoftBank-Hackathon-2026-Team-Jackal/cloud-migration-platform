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
        inner = AnthropicLLMClient(
            model=config.llm_model,
            max_tokens=config.llm_max_output_tokens,
            effort=EFFORT.get(role, "medium"),
        )
        return GovernedLLMClient(
            inner,
            ledger,
            run_id=migration_id,
            model=config.llm_model,
            max_output_tokens=config.llm_max_output_tokens,
        )

    return make
