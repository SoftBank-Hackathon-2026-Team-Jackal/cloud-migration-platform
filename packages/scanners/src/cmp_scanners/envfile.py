"""Parse `.env` files for variable names only; values are never kept."""

import re

_NAME = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def parse_env_names(text: str) -> list[str]:
    names: set[str] = set()
    for line in text.splitlines():
        match = _NAME.match(line)
        if match:
            names.add(match.group(1))
    return sorted(names)


def is_env_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")
