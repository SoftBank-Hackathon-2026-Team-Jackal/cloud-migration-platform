"""Single-variable PostgreSQL connection URL (ticket 19, #79; D-16 decisions U2/U3).

Shared by Sandbox (analysis 11) and Provisioning so both build the same format. The URL holds the
password, so it only ever lives in a Kubernetes Secret (`<DB release>-url`, key `url`); Values,
Plans, Previews, job payloads and logs carry the Secret reference only.
"""

import re
from urllib.parse import quote

URL_SECRET_KEY = "url"
_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def connection_url_secret_name(database_release: str) -> str:
    """DB release names are at most 53 characters, so the Secret name stays within 63."""
    return f"{database_release}-url"


def connection_url(*, username: str, password: str, host: str, port: int, database: str) -> str:
    """`postgresql://<user>:<password>@<host>:<port>/<db>` with RFC 3986 percent-encoding of
    user, password and database name; no query parameters (in-cluster, no TLS)."""
    if not username or not password or not database:
        raise ValueError("INVALID_CONNECTION_PART")
    if not _HOST.fullmatch(host):
        raise ValueError("INVALID_CONNECTION_HOST")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("INVALID_CONNECTION_PORT")
    user, secret, name = (quote(part, safe="") for part in (username, password, database))
    return f"postgresql://{user}:{secret}@{host}:{port}/{name}"
