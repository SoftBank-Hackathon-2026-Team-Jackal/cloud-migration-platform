"""Go language adapter for `scan_code` (first supported language, D-07).

Line-based heuristics, not a Go parser: it records names, files and lines only. Calls built
at runtime (concatenated service names, URLs from variables) are not visible here.
"""

import re

from cmp_scanners.code import CallSite

_ENV_READ = re.compile(
    r"\b(?:os\.Getenv|os\.LookupEnv|getenv|getEnv|mustGetenv|mustEnv|envOr|envString|envInt)"
    r'\(\s*"([A-Za-z_][A-Za-z0-9_]*)"'
)
_IMPORT_LINE = re.compile(r'^\s*(?:import\s+)?(?:([A-Za-z_][A-Za-z0-9_]*|_|\.)\s+)?"([^"]+)"\s*$')
_AWS_SERVICE_IMPORT = re.compile(r"^github\.com/aws/aws-sdk-go(?:-v2)?/service/([a-z0-9]+)")
_AWS_INPUT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Z][A-Za-z0-9]*)Input\b")
_HTTP_CALL = re.compile(
    r"\bhttp\.(?:Get|Post|Head|PostForm|NewRequest|NewRequestWithContext)\(.*?\"https?://"
    r"(?:[^\"/?#\s@]*@)?([^\"/?#\s@]+)"
)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")
_DB_DRIVERS = (
    "github.com/jackc/pgx",
    "github.com/lib/pq",
    "gorm.io/driver/postgres",
    "github.com/uptrace/bun/driver/pgdriver",
)


class GoAdapter:
    language = "go"
    extensions = (".go",)

    def scan_file(self, path: str, text: str) -> list[CallSite]:
        sites: list[CallSite] = []
        aliases: dict[str, str] = {}  # import alias or package name -> AWS service
        in_imports = False
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if line.startswith("//"):
                continue
            if line.startswith("import ("):
                in_imports = True
                continue
            if in_imports and line == ")":
                in_imports = False
                continue
            if in_imports or line.startswith("import "):
                match = _IMPORT_LINE.match(raw)
                if match:
                    sites.extend(self._import_sites(path, number, match, aliases))
                continue
            sites.extend(self._code_sites(path, number, raw, aliases))
        return sites

    @staticmethod
    def _import_sites(path, number, match, aliases) -> list[CallSite]:
        alias, module = match.group(1), match.group(2)
        found = []
        service = _AWS_SERVICE_IMPORT.match(module)
        if service:
            aliases[alias or service.group(1)] = service.group(1)
            found.append(
                CallSite(
                    kind="AWS_SDK", target=service.group(1), file=path, line=number, detail="import"
                )
            )
        if module.startswith(_DB_DRIVERS):
            found.append(CallSite(kind="DB_DRIVER", target=module, file=path, line=number))
        return found

    @staticmethod
    def _code_sites(path, number, raw, aliases) -> list[CallSite]:
        found = []
        for match in _ENV_READ.finditer(raw):
            found.append(CallSite(kind="ENV_READ", target=match.group(1), file=path, line=number))
        for match in _AWS_INPUT.finditer(raw):
            service = aliases.get(match.group(1))
            if service:
                found.append(
                    CallSite(
                        kind="AWS_SDK",
                        target=f"{service}.{match.group(2)}",
                        file=path,
                        line=number,
                        detail="call",
                    )
                )
        for match in _HTTP_CALL.finditer(raw):
            host = match.group(1)
            if host.split(":")[0].lower() not in _LOCAL_HOSTS:
                found.append(CallSite(kind="EXTERNAL_HOST", target=host, file=path, line=number))
        return found
