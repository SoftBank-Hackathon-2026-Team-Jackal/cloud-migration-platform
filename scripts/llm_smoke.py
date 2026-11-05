"""One short Claude call through the spend ledger, to check the key, the model and the books.

Costs a small amount of real money (a few hundredths of a cent), so it only runs with `--yes`.
The key is read from ANTHROPIC_API_KEY by the SDK; this script never prints it.

  ANTHROPIC_API_KEY=... uv run python scripts/llm_smoke.py --yes
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from cmp_agents.anthropic_client import AnthropicLLMClient
from cmp_agents.cost import CostLedger, GovernedLLMClient, Limits
from cmp_agents.llm import LLMError, Message


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="confirm that this spends real money")
    args = parser.parse_args()
    if not args.yes:
        print("Refusing to call the API without --yes (it costs a little money).")
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        ledger = CostLedger(
            Path(tmp) / "ledger.json",
            Limits(per_run_usd=0.05, per_day_usd=0.05, per_month_usd=0.05),
        )
        client = GovernedLLMClient(
            AnthropicLLMClient(max_tokens=300, effort="low"),
            ledger, run_id="smoke", model="claude-sonnet-5-5", max_output_tokens=300,
        )  # fmt: skip
        try:
            reply = client.complete(
                system="Answer with one short word.",
                messages=[Message(role="user", content="Say OK.")],
                tools=[],
            )
        except LLMError as exc:
            print(f"Call failed: {exc.code}")
            return 1
        spent = ledger.spent("smoke")["run"]
        print(f"reply={reply.text!r} in={reply.input_tokens} out={reply.output_tokens} "
              f"cache_read={reply.cache_read_tokens} booked=${spent:.6f}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
