"""Check that the real first cycle can start (no cost, no changes).

  uv run python scripts/preflight.py [--repository /path/to/demo-app]

Exits 1 when something required is missing. The model key is checked for presence only.
"""

import argparse
import os
import sys

from cmp_worker.config import settings
from cmp_worker.preflight import FAIL, OK, WARN, ok, run_checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository")
    args = parser.parse_args()
    checks = run_checks(
        settings, api_store_dir=os.environ.get("ARTIFACT_STORE_DIR"), repository=args.repository
    )
    marks = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    for check in checks:
        print(
            f"[{marks[check.status]}] {check.name}" + (f" - {check.detail}" if check.detail else "")
        )
    return 0 if ok(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
