"""Access to a private source repository, behind one interface (ticket 23).

The platform will keep the user's GitHub token after the user logs in with GitHub. Until that
exists, an operator can supply a token for development. Both are the same thing to the code that
needs a token: it holds a `VersionRef` (a name, never a secret), and asks a resolver for the token
at the moment it clones or pushes. Nothing but the resolver ever sees the token's source, and
the token is never put in a Workflow input, history, log, result or error message.

A resolver that reads the platform's token store implements `SourceTokenResolver`; the code that
clones (and later opens pull requests) does not change.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from cmp_contracts.provisioning import VersionRef

Purpose = Literal["READ", "WRITE"]  # clone a repository / push a branch and open a pull request

# A name for the operator-supplied token. The environment variable is fixed in code, so an
# analysis input cannot make a worker read any other variable.
DEV_REF_ID = "dev-env"
DEV_TOKEN_ENV = "CMP_GITHUB_TOKEN"

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TOKEN = re.compile(r"^[\x21-\x7e]{8,512}$")  # printable, no spaces or control characters


class SourceAccessError(Exception):
    """`code` is stable and safe to show. The message never carries a token."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceCredential:
    """A token for a host. Printing it, or putting it in a log line, shows nothing."""

    token: str = field(repr=False)
    host: str = "github.com"

    def __repr__(self) -> str:
        return f"SourceCredential(host={self.host!r}, token=***)"

    __str__ = __repr__


class SourceTokenResolver(Protocol):
    """Turns a reference into a credential.

    Contract for every implementation:
    - read-only: it never stores, changes or revokes anything;
    - `SOURCE_ACCESS_NOT_CONFIGURED` when no token store is set up, `SOURCE_TOKEN_UNAVAILABLE`
      when the reference has no usable token (unknown, revoked, expired), `SOURCE_ACCESS_DENIED`
      when the token may not be used for `purpose`;
    - it never logs or returns the token anywhere but in the `SourceCredential`.
    """

    def resolve(self, ref: VersionRef, *, host: str, purpose: Purpose) -> SourceCredential: ...


class NoSourceTokenResolver:
    """Default: no token store is configured, so a private source cannot be read."""

    def resolve(self, ref: VersionRef, *, host: str, purpose: Purpose) -> SourceCredential:
        raise SourceAccessError("SOURCE_ACCESS_NOT_CONFIGURED")


class EnvSourceTokenResolver:
    """Development: one token from the worker's own environment (`CMP_GITHUB_TOKEN`)."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve(self, ref: VersionRef, *, host: str, purpose: Purpose) -> SourceCredential:
        if ref.id != DEV_REF_ID or host != "github.com":
            raise SourceAccessError("SOURCE_TOKEN_UNAVAILABLE")
        token = self._environ.get(DEV_TOKEN_ENV, "")
        if not _TOKEN.fullmatch(token):
            raise SourceAccessError("SOURCE_TOKEN_UNAVAILABLE")
        return SourceCredential(token=token, host=host)


def dev_ref() -> VersionRef:
    return VersionRef(id=DEV_REF_ID, version="1")


def github_repository(url: str) -> tuple[str, str]:
    """`https://github.com/<owner>/<repo>[.git]` and nothing else; returns (owner, repo).

    A token is only ever sent to a repository that passes this check: no other host, no user
    information in the address, no port, query or fragment, no path tricks.
    """
    prefix = "https://github.com/"
    if not url.startswith(prefix) or "@" in url or "?" in url or "#" in url or ":" in url[8:]:
        raise SourceAccessError("SOURCE_REPOSITORY_NOT_ALLOWED")
    parts = url.removeprefix(prefix).rstrip("/").removesuffix(".git").split("/")
    if len(parts) != 2:
        raise SourceAccessError("SOURCE_REPOSITORY_NOT_ALLOWED")
    owner, repo = parts
    if not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo) or repo in {".", ".."}:
        raise SourceAccessError("SOURCE_REPOSITORY_NOT_ALLOWED")
    return owner, repo


def build_source_resolver(mode: str) -> SourceTokenResolver:
    """`none` (default) or `env` (development). The platform's token store plugs in here."""
    if mode == "env":
        return EnvSourceTokenResolver()
    if mode == "none":
        return NoSourceTokenResolver()
    raise ValueError("SOURCE_TOKEN_RESOLVER_UNKNOWN")
