"""Send review decisions to the analysis child Workflow (demo CLI, no API needed).

  review_signal.py state    <migration_id>
  review_signal.py confirm  <migration_id> --version 1 --actor me --required a=true b=false
  review_signal.py approve  <migration_id> --version 1 --actor me --approve b=APPROVED c=REJECTED

The child is queried first and a Signal is sent only while it waits for that decision.
`--version` is required so a decision never lands on a different analysis version.
"""

import argparse
import asyncio
import json
import sys

from cmp_worker.config import settings
from cmp_worker.review_client import (
    ReviewNotPendingError,
    get_review_state,
    parse_approvals,
    parse_required,
    send_review_signal,
)
from temporalio.client import Client


async def main(args: argparse.Namespace) -> int:
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    try:
        state = await get_review_state(client, args.migration_id)
        if args.command == "state":
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        ref = {
            "id": state["analysis_ref"]["id"] if state["analysis_ref"] else "",
            "version": args.version,
        }
        if args.command == "confirm":
            payload = {
                "analysis_ref": ref,
                "required": parse_required(args.required),
                "operational": args.operational,
                "actor": args.actor,
            }
        else:
            payload = {
                "analysis_ref": ref,
                "approvals": parse_approvals(args.approve),
                "actor": args.actor,
            }
        await send_review_signal(client, args.migration_id, args.command, payload)
        print("signal sent; run `state` to see whether it was accepted")
        return 0
    except (ReviewNotPendingError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("state").add_argument("migration_id")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("migration_id")
    confirm.add_argument("--version", required=True)
    confirm.add_argument("--actor", required=True)
    confirm.add_argument("--required", nargs="+", required=True, metavar="ROW=true|false")
    confirm.add_argument("--operational", nargs="*", default=[])
    approve = sub.add_parser("approve")
    approve.add_argument("migration_id")
    approve.add_argument("--version", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--approve", nargs="+", required=True, metavar="ROW=APPROVED|REJECTED")
    return root


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parser().parse_args())))
