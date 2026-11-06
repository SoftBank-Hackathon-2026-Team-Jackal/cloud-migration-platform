"""The `sslmode` a PostgreSQL connection asks for, found in text (ticket 28-B).

Only the mode word is kept: the connection string around it (user, password, host) never leaves
this function. `.env` and Secret values are not read by the scanners at all (AV-020), so a mode
that lives only there is reported as unknown by the measure, not guessed.
"""

import re
from collections.abc import Iterator

# libpq's modes. `disable`, `allow` and `prefer` can end up on a plaintext connection.
PLAINTEXT_MODES = frozenset({"disable", "allow", "prefer"})
ENCRYPTED_MODES = frozenset({"require", "verify-ca", "verify-full"})
SSL_MODES = PLAINTEXT_MODES | ENCRYPTED_MODES

_MODE = "|".join(sorted(SSL_MODES, key=len, reverse=True))
_IN_TEXT = re.compile(rf"(?i)\bsslmode\s*[=:]\s*[\"']?({_MODE})(?![\w-])")
_ENV_NAME = re.compile(r"(?i)^(?:pg|db_|database_|postgres_)?ssl_?mode$")


def modes_in(text: str) -> list[str]:
    """Every `sslmode=<mode>` in a text, lower case, in order."""
    return [match.group(1).lower() for match in _IN_TEXT.finditer(text)]


def mode_lines(text: str) -> Iterator[tuple[int, str]]:
    """(line number, mode) for each `sslmode=<mode>` in a source text."""
    for number, line in enumerate(text.splitlines(), start=1):
        for mode in modes_in(line):
            yield number, mode


def env_mode(name: str, value: str) -> str | None:
    """The mode an environment variable sets: a mode-named variable with a mode value."""
    if _ENV_NAME.fullmatch(name) and value.strip().lower() in SSL_MODES:
        return value.strip().lower()
    return None
