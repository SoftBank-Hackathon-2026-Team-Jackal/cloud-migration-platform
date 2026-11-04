"""Role prompts live in `prompts/<role>.md`; the version is a hash of the text."""

import hashlib
import re
from pathlib import Path

_DIR = Path(__file__).parent / "prompts"
_ROLE = re.compile(r"^[a-z][a-z0-9_-]*$")


def load_prompt(role: str) -> tuple[str, str]:
    if not _ROLE.fullmatch(role):
        raise FileNotFoundError(f"no prompt for role '{role}'")
    path = _DIR / f"{role}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no prompt for role '{role}'")
    text = path.read_text()
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]
