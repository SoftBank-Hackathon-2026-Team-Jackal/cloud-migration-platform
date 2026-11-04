"""Mask secret-looking strings before source reaches an LLM (AV-020)."""

import math
import re
from pathlib import Path

from cmp_scanners.envfile import _NAME, is_env_file

MASK = "[MASKED]"
SKIP_DIRS = {".git"}
MAX_FILE_BYTES = 1_000_000

_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_KNOWN = [
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]
_URL_CREDENTIAL = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")
_KEY_NAME = r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)"
_QUOTED_ASSIGN = re.compile(
    rf"(?i)(\b[A-Za-z0-9_.-]*{_KEY_NAME}[A-Za-z0-9_.-]*\s*[:=]\s*)(['\"])([^'\"\n]{{4,}})\2"
)
_BARE_ASSIGN = re.compile(
    rf"(?im)(\b[A-Za-z0-9_.-]*{_KEY_NAME}[A-Za-z0-9_.-]*\s*[:=]\s*)([A-Za-z0-9+/=_@.-]{{6,}})\s*$"
)
_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{24,}")
_HEX_IDS = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CODE_PREFIXES = ("os.", "process.", "env.", "config.", "settings.", "self.", "$", "{")


def _entropy(value: str) -> float:
    counts = {c: value.count(c) for c in set(value)}
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in counts.values())


def _mask_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if _HEX_IDS.fullmatch(token):
        return token
    if not (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
        return token
    return MASK if _entropy(token) >= 4.0 else token


def _bare(match: re.Match[str]) -> str:
    value = match.group(2)
    return match.group(1) + (value if value.startswith(_CODE_PREFIXES) else MASK)


def mask_text(text: str) -> str:
    text = _PEM.sub(MASK, text)
    for pattern in _KNOWN:
        text = pattern.sub(MASK, text)
    text = _URL_CREDENTIAL.sub(MASK, text)
    text = _QUOTED_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(2)}", text)
    text = _BARE_ASSIGN.sub(_bare, text)
    return _TOKEN.sub(_mask_token, text)


def _mask_env_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        match = _NAME.match(line)
        lines.append(f"{match.group(1)}={MASK}" if match else mask_text(line))
    return "\n".join(lines) + "\n"


def mask_tree(source: Path, destination: Path) -> None:
    """Copy `source` to `destination` with secrets masked; `.git` and binaries are skipped."""
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if SKIP_DIRS.intersection(relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(_mask_env_text(text) if is_env_file(path.name) else mask_text(text))
