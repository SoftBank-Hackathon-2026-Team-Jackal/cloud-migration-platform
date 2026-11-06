#!/usr/bin/env python3
"""Demo approver credential: create, grant, rotate, revoke (ticket 07 D2, infra/demo/README.md).

Runs in the SSM session against the host-only API (127.0.0.1:8000), standard library only.
The admin key comes only from the APPROVAL_ADMIN_KEY environment variable or a hidden prompt,
never from an argument. The issued credential is written once: to stdout, or with --env-file
straight into the web env file (0600) as PLATFORM_APPROVER_TOKEN without printing it.
"""

import argparse
import getpass
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_NAME = "PLATFORM_APPROVER_TOKEN"


def admin_key() -> str:
    key = os.environ.get("APPROVAL_ADMIN_KEY") or getpass.getpass("APPROVAL_ADMIN_KEY: ")
    if not key:
        sys.exit("APPROVAL_ADMIN_KEY is required")
    return key


def call(api: str, method: str, path: str, key: str, payload: dict | None = None) -> dict | None:
    request = urllib.request.Request(
        api.rstrip("/") + path,
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"X-Approval-Admin-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # Error bodies are codes only (APPROVER_EXISTS, APPROVER_NOT_FOUND, ...); never a credential.
        sys.exit(f"{method} {path}: HTTP {exc.code} {exc.read().decode(errors='replace')[:200]}")
    return json.loads(body) if body else None


def write_env(path: Path, value: str) -> None:
    """Replace or append ENV_NAME in a 0600 env file atomically; other lines stay as they are."""
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines if not line.startswith(f"{ENV_NAME}=")]
    kept.append(f"{ENV_NAME}={value}")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as target:
            target.write("\n".join(kept) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["create", "grant", "rotate", "revoke"])
    parser.add_argument("approver_id")
    parser.add_argument(
        "--migration",
        action="append",
        help="create/grant: Migration ID this approver may decide (repeatable, required).",
    )
    parser.add_argument("--api", default=os.environ.get("CMP_API_BASE", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--env-file", type=Path, help=f"write {ENV_NAME} into this file instead of stdout"
    )
    args = parser.parse_args(argv)
    key = admin_key()
    approver = urllib.parse.quote(args.approver_id, safe="")

    if args.action in ("create", "grant") and not args.migration:
        parser.error("--migration is required for create and grant")
    if args.action == "grant":
        result = call(
            args.api,
            "POST",
            f"/provisioning/approvers/{approver}/migrations",
            key,
            {"migration_ids": args.migration},
        )
        print(f"{args.approver_id} may decide: {', '.join(result['migration_ids'])}")
        return
    if args.action == "revoke":
        call(args.api, "DELETE", f"/provisioning/approvers/{approver}/credential", key)
        print(
            f"revoked {args.approver_id}: the previous credential now returns 401", file=sys.stderr
        )
        return
    if args.action == "create":
        result = call(
            args.api,
            "POST",
            "/provisioning/approvers",
            key,
            {"approver_id": args.approver_id, "migration_ids": args.migration},
        )
    else:
        result = call(args.api, "POST", f"/provisioning/approvers/{approver}/credential", key)
    credential = result["credential"]
    if args.env_file:
        write_env(args.env_file, credential)
        print(
            f"{args.action}d {args.approver_id}: {ENV_NAME} written to {args.env_file}",
            file=sys.stderr,
        )
    else:
        print(credential)


if __name__ == "__main__":
    main()
