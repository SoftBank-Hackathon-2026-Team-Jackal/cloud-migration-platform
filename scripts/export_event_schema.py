"""Print the migration event JSON Schema in the web contract layout (ticket 24).

    uv run python scripts/export_event_schema.py [--out PATH]

The web copy lives in docs/contracts/events.schema.json; tests/test_event_contract.py compares
the two in a normal form. When the web contract changes, refresh the copy from
cloud-migration-web `contract/events.schema.json`, then update `cmp_contracts.events`.
"""

import argparse
import json
from pathlib import Path

from cmp_contracts.events import event_json_schema

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--out", type=Path, help="write to this file instead of stdout")
args = parser.parse_args()
text = json.dumps(event_json_schema(), ensure_ascii=False, indent=2) + "\n"
if args.out:
    args.out.write_text(text, encoding="utf-8")
    print(args.out)
else:
    print(text, end="")
