"""Language-independent code scan (AV-003): call sites, extensions, `.env` comparison.

Language adapters plug in through `LanguageAdapter`. Results hold names, files and lines
only; source text is never copied into them.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cmp_scanners.envfile import is_env_file
from cmp_scanners.sslmode import mode_lines

MAX_FILE_BYTES = 1_000_000
_SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "__pycache__", "dist"}
_SOURCE_LANGUAGES = {
    ".go": "go", ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".java": "java",
    ".kt": "kotlin", ".scala": "scala", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".rs": "rust", ".c": "c", ".cpp": "cpp", ".swift": "swift",
}  # fmt: skip
_CREATE_EXTENSION = re.compile(
    r"\bcreate\s+extension\s+(?:if\s+not\s+exists\s+)?(?:\"([^\"]+)\"|([A-Za-z0-9_-]+))",
    re.IGNORECASE,
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CallSite(_Model):
    kind: Literal["AWS_SDK", "ENV_READ", "DB_DRIVER", "EXTERNAL_HOST"]
    target: str
    file: str
    line: int
    detail: str | None = None


class Unscannable(_Model):
    file: str
    language: str | None
    reason: Literal["NO_ADAPTER", "ADAPTER_ERROR", "TOO_LARGE", "NOT_TEXT"]


class ExtensionCreation(_Model):
    name: str
    file: str
    line: int


class EnvComparison(_Model):
    only_in_env: list[str]
    missing_from_env: list[str]
    # True when some source files were not scanned, so "unused" names may be wrong.
    partial: bool


class SslModeSite(_Model):
    """A `sslmode` mode word in source code (ticket 28-B). Only the mode, never the string."""

    mode: str
    file: str
    line: int


class CodeScan(_Model):
    call_sites: list[CallSite]
    unscannable: list[Unscannable]
    extension_creations: list[ExtensionCreation]
    env_comparison: EnvComparison
    ssl_modes: list[SslModeSite] = []  # older stored scans have none

    @property
    def creates_extensions(self) -> bool:
        return bool(self.extension_creations)


class LanguageAdapter(Protocol):
    language: str
    extensions: tuple[str, ...]

    def scan_file(self, path: str, text: str) -> list[CallSite]: ...


def _files(root: Path) -> Iterable[tuple[Path, str]]:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _SKIP_DIRS.intersection(relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if is_env_file(path.name):
            continue
        yield path, relative.as_posix()


def _read(path: Path) -> tuple[str | None, str | None]:
    if path.stat().st_size > MAX_FILE_BYTES:
        return None, "TOO_LARGE"
    try:
        return path.read_text(), None
    except UnicodeDecodeError:
        return None, "NOT_TEXT"


def _extension_creations(text: str, file: str) -> list[ExtensionCreation]:
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _CREATE_EXTENSION.search(line)
        if match and "--" not in line[: match.start()]:
            found.append(
                ExtensionCreation(name=match.group(1) or match.group(2), file=file, line=number)
            )
    return found


def _env_names(env_names: Mapping[str, Sequence[str]] | Iterable[str]) -> set[str]:
    if isinstance(env_names, Mapping):
        return {name for names in env_names.values() for name in names}
    return set(env_names)


def scan_code(
    workspace: Path | str,
    env_names: Mapping[str, Sequence[str]] | Iterable[str],
    adapters: Sequence[LanguageAdapter],
) -> CodeScan:
    root = Path(workspace)
    if not root.is_dir():
        raise FileNotFoundError(root)
    by_extension = {ext: adapter for adapter in adapters for ext in adapter.extensions}
    sites: list[CallSite] = []
    unscannable: list[Unscannable] = []
    extensions: list[ExtensionCreation] = []
    ssl_modes: list[SslModeSite] = []

    for path, relative in _files(root):
        suffix = path.suffix.lower()
        adapter = by_extension.get(suffix)
        language = adapter.language if adapter else _SOURCE_LANGUAGES.get(suffix)
        if adapter is None and language is None and suffix != ".sql":
            continue
        text, problem = _read(path)
        if text is None:
            unscannable.append(Unscannable(file=relative, language=language, reason=problem))
            continue
        extensions.extend(_extension_creations(text, relative))
        ssl_modes.extend(
            SslModeSite(mode=mode, file=relative, line=number) for number, mode in mode_lines(text)
        )
        if adapter is None:
            if language is not None:
                unscannable.append(
                    Unscannable(file=relative, language=language, reason="NO_ADAPTER")
                )
            continue
        try:
            sites.extend(adapter.scan_file(relative, text))
        except Exception:  # noqa: BLE001 - one broken adapter must not abort the scan
            unscannable.append(
                Unscannable(file=relative, language=language, reason="ADAPTER_ERROR")
            )

    declared = _env_names(env_names)
    read = {site.target for site in sites if site.kind == "ENV_READ"}
    return CodeScan(
        call_sites=sites,
        unscannable=unscannable,
        extension_creations=extensions,
        ssl_modes=ssl_modes,
        env_comparison=EnvComparison(
            only_in_env=sorted(declared - read),
            missing_from_env=sorted(read - declared),
            partial=bool(unscannable),
        ),
    )
