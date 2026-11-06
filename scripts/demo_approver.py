"""Demo approver issuance and per-Migration grant (ticket 60). Operator use only.

    APPROVAL_ADMIN_KEY=... uv run python scripts/demo_approver.py issue \
        --api https://<platform> --migration-id <migration-id> --out ~/.cmp/demo-approver.token
    APPROVAL_ADMIN_KEY=... uv run python scripts/demo_approver.py grant \
        --api https://<platform> --migration-id <new migration-id>

`issue` calls `POST /provisioning/approvers` and writes the returned credential to `--out`
(created 0600, never overwritten). `grant` calls `POST /provisioning/approvers/{id}/migrations`
so the same credential can decide a newly started Migration. Neither prints the credential or the
operator key; the operator key is read from the environment only (never a command-line value).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

DEFAULT_APPROVER = "demo-approver"
DEFAULT_OUT = Path.home() / ".cmp" / "demo-approver.token"
Post = Callable[[str, dict, str], tuple[int, dict]]


def http_post(url: str, body: dict, admin_key: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Approval-Admin-Key": admin_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def write_secret(path: Path, text: str) -> None:
    """Create `path` 0600 (parents 0700); refuse to replace an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text + "\n")


def _detail(body: dict) -> str:
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code", ""))
    return detail if isinstance(detail, str) else ""


def main(argv: Sequence[str] | None = None, post: Post = http_post) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["issue", "grant"])
    parser.add_argument("--api", required=True, help="platform base URL (API origin)")
    parser.add_argument("--approver-id", default=DEFAULT_APPROVER)
    parser.add_argument("--migration-id", action="append", required=True, dest="migrations")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="credential file (issue)")
    args = parser.parse_args(argv)

    admin_key = os.environ.get("APPROVAL_ADMIN_KEY")
    if not admin_key:
        print("APPROVAL_ADMIN_KEY is not set", file=sys.stderr)
        return 2
    api = args.api.rstrip("/")
    if args.command == "issue":
        out = args.out.expanduser()
        if out.exists():
            print(f"{out} exists; use a new --approver-id and --out to rotate", file=sys.stderr)
            return 1
        status, body = post(
            f"{api}/provisioning/approvers",
            {"approver_id": args.approver_id, "migration_ids": args.migrations},
            admin_key,
        )
        if status != 201 or not str(body.get("credential", "")):
            print(f"approver not issued: HTTP {status} {_detail(body)}", file=sys.stderr)
            return 1
        write_secret(out, body["credential"])
        print(f"approver {args.approver_id} issued; credential written to {out} (0600)")
        return 0

    status, body = post(
        f"{api}/provisioning/approvers/{args.approver_id}/migrations",
        {"migration_ids": args.migrations},
        admin_key,
    )
    if status != 200:
        print(f"migrations not granted: HTTP {status} {_detail(body)}", file=sys.stderr)
        return 1
    print(f"approver {args.approver_id} may decide: {', '.join(body['migration_ids'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
