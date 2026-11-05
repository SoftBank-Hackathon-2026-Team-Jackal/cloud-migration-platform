"""EnvPlan builder for analysis (ticket ENV-03 #147, used by ENV-05).

A convenience, not the rule owner: analysis decides the final rules in ENV-05. It reuses the
existing patterns of `cmp_scanners.assembly` without changing that module, so the resulting
`EnvExceptions` match `build_requirements` on the same inputs:

- EXCLUDED: names starting with `AWS_` (source-only credentials)
- REPLACED: `_BINDING_PATTERNS` (only when the app has a database)
- MISSING: read by the code but not declared
- UNREPRESENTABLE: `_DSN_VARIABLE` key=value DSNs (only when the app has a database)
- PASS: everything else

The agreed table in `specs/env-file-ref/spec.md` is the minimum; the regexes also accept the prefix
variants `DB|DATABASE|POSTGRES(QL)|PG` with an optional underscore (for example `PG_HOST`).
Precedence follows `build_requirements`: EXCLUDED > REPLACED > MISSING > UNREPRESENTABLE > PASS.
"""

from collections.abc import Iterable

from cmp_contracts.env_plan import EnvPlan, EnvPlanEntry
from cmp_contracts.provisioning import ContentRef
from cmp_scanners.assembly import _BINDING_PATTERNS, _DSN_VARIABLE

SOURCE_ONLY_PREFIX = "AWS_"


def _binding(name: str) -> str | None:
    return next((b for b, pattern in _BINDING_PATTERNS.items() if pattern.match(name)), None)


def build_env_plan(
    env_file_ref: ContentRef,
    env_names: Iterable[str],
    code_env_reads: Iterable[str],
    *,
    has_database: bool = True,
) -> EnvPlan:
    """`env_names`: declared names (`resolve_env_names` plus, like `build_requirements`, names the
    app manifests inject). `code_env_reads`: names the code reads (`ENV_READ` call sites)."""
    declared, reads = set(env_names), set(code_env_reads)
    entries = []
    for key in sorted(declared | reads):
        binding = _binding(key) if has_database else None
        if key.startswith(SOURCE_ONLY_PREFIX):
            entry = EnvPlanEntry(key=key, disposition="EXCLUDED", reason="SOURCE_ONLY_CREDENTIAL")
        elif binding:
            entry = EnvPlanEntry(
                key=key, disposition="REPLACED", binding=binding, reason="DB_OUTPUT_BINDING"
            )
        elif key not in declared:
            entry = EnvPlanEntry(key=key, disposition="MISSING", reason="NOT_DECLARED")
        elif has_database and _DSN_VARIABLE.match(key):
            entry = EnvPlanEntry(key=key, disposition="UNREPRESENTABLE", reason="DSN_FORMAT")
        else:
            entry = EnvPlanEntry(key=key, disposition="PASS")
        entries.append(entry)
    return EnvPlan(env_file_ref=env_file_ref, entries=entries)
