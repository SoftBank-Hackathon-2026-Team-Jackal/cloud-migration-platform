"""Security probe cases made from the requirements' network intent (ticket 30).

The cases hold no name, port or path of any one application. For every database in the
requirements, each resource that the intent lets reach it must connect, and a Pod that is none of
them must be blocked; the port is the intent's. The expected answers come from the intent and from
nothing a person or a model wrote.

Templates that have nothing to test against yet are listed with why, so a missing probe is stated
and not silently absent: the platform's database offers no SSL to reject plaintext against, and
which paths are administrative is for a person to confirm (ticket 29).
"""

import re
from dataclasses import dataclass, field

from cmp_contracts.provisioning_v2 import RequirementsV2

from cmp_testsuite.model import ProbeStep, SuiteCase

# (template, why there is no case). The reasons are codes.
SKIPPED_TEMPLATES = (
    ("DB_SSL_REJECT", "TARGET_DB_SSL_NOT_CONFIGURED"),
    ("ADMIN_PATH_BLOCKED", "ADMIN_PATH_NOT_CONFIRMED"),
)
_COVERS = ["network-policy"]


@dataclass(frozen=True)
class ProbePlan:
    cases: list[SuiteCase]
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _case(case_id: str, description: str, steps: list[ProbeStep]) -> SuiteCase:
    return SuiteCase(
        id=case_id,
        description=description,
        source="STANDARD",
        effect="READ_ONLY",
        covers=list(_COVERS),
        steps=steps,
    )


def security_probe_cases(requirements: RequirementsV2) -> ProbePlan:
    skipped: list[tuple[str, str]] = list(SKIPPED_TEMPLATES)
    databases = sorted(r.id for r in requirements.resources if r.kind == "POSTGRESQL")
    if not databases:
        return ProbePlan([], [("DB_ACCESS", "NO_DATABASE_IN_REQUIREMENTS"), *skipped])
    cases: list[SuiteCase] = []
    for database in databases:
        edges = sorted(
            {
                (i.source_resource_id, i.port)
                for i in requirements.network_intent
                if i.destination_resource_id == database and i.protocol == "TCP"
            }
        )
        if not edges:
            skipped.append(("DB_ACCESS", "NO_INTENT_TO_DATABASE"))
            continue
        ports_by_source: dict[str, list[int]] = {}
        for source, port in edges:
            ports_by_source.setdefault(source, []).append(port)
        for source, ports in sorted(ports_by_source.items()):
            for port in ports:
                tag = f"-{port}" if len(ports) > 1 else ""
                cases.append(
                    _case(
                        f"sec-db-allowed-{_slug(source)}-{_slug(database)}{tag}",
                        f"{source} can reach {database} as the network intent allows.",
                        [
                            ProbeStep(
                                role="ALLOWED_CLIENT",
                                source_resource_id=source,
                                target_resource_id=database,
                                port=port,
                                expect="CONNECTS",
                            )
                        ],
                    )
                )
        ports = sorted({port for _, port in edges})
        cases.append(
            _case(
                f"sec-db-outsider-{_slug(database)}",
                f"A pod outside the network intent cannot reach {database}.",
                [
                    ProbeStep(
                        role="OUTSIDER",
                        target_resource_id=database,
                        port=port,
                        expect="BLOCKED",
                    )
                    for port in ports
                ],
            )
        )
    cases.sort(key=lambda c: (c.id.startswith("sec-db-outsider"), c.id))
    return ProbePlan(cases, skipped)
