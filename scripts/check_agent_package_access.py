"""Check that a released Agent image and Chart can be pulled anonymously (ticket 42, #177).

Asks GHCR for an anonymous pull token and reads the release manifest of
`ghcr.io/<owner>/cmp-onprem-agent:<version>` and `ghcr.io/<owner>/charts/onprem-agent:<version>`
without any credential (what a user cluster and `helm install oci://...` do). Prints one line per
package (`PUBLIC`, `PRIVATE`, `NOT_FOUND`, `ERROR <status>`) and exits 0 only when both are
public. Read-only; needs no token:

    uv run python scripts/check_agent_package_access.py --version 0.3.0
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

REGISTRY = "https://ghcr.io"
OWNER = "softbank-hackathon-2026-team-jackal"
PACKAGES = ("cmp-onprem-agent", "charts/onprem-agent")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")
ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)

Fetch = Callable[[str, dict[str, str]], tuple[int, bytes]]


def http_fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _status(owner: str, package: str, version: str, fetch: Fetch) -> str:
    repository = f"{owner}/{package}"
    scope = urllib.parse.quote(f"repository:{repository}:pull", safe=":/")
    code, body = fetch(f"{REGISTRY}/token?scope={scope}", {})
    if code in (401, 403):
        return "PRIVATE"
    if code != 200:
        return f"ERROR {code}"
    try:
        token = json.loads(body).get("token", "")
    except ValueError:
        return "ERROR TOKEN_RESPONSE"
    if not token:
        return "PRIVATE"
    headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT}
    code, _ = fetch(f"{REGISTRY}/v2/{repository}/manifests/{version}", headers)
    if code == 200:
        return "PUBLIC"
    if code in (401, 403):
        return "PRIVATE"
    if code == 404:
        return "NOT_FOUND"
    return f"ERROR {code}"


def check(version: str, owner: str = OWNER, fetch: Fetch = http_fetch) -> dict[str, str]:
    if not SEMVER.fullmatch(version):
        raise ValueError("INVALID_VERSION")
    owner = owner.lower()  # GHCR paths are lowercase
    return {package: _status(owner, package, version, fetch) for package in PACKAGES}


def main(argv: list[str] | None = None, fetch: Fetch = http_fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--owner", default=OWNER)
    args = parser.parse_args(argv)
    try:
        report = check(args.version, args.owner, fetch)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for package, status in report.items():
        print(f"ghcr.io/{args.owner.lower()}/{package}:{args.version} {status}")
    return 0 if all(status == "PUBLIC" for status in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
