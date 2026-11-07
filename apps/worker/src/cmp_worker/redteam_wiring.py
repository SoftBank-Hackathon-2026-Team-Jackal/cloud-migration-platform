"""Builds the `RedTeamKit` a Sandbox Worker hands to the loop (ticket 38).

Kept out of `redteam_stage` so that the Workflow-facing pieces do not import the cluster or model
code. Nothing here runs on its own: the kit only acts when a Workflow asks for a run that a person
has approved.
"""

import base64
import binascii
import json
from collections.abc import Callable

from cmp_agents.llm import LLMClient
from cmp_agents.redteam_agent import AllowList

from cmp_worker.artifact_store import ArtifactStore
from cmp_worker.redteam_brain import Brain
from cmp_worker.redteam_safeguards import InMemoryApprovalLedger, RedTeamRunner
from cmp_worker.redteam_stage import RedTeamKit
from cmp_worker.redteam_tools import TOOL_NAMES, make_dispatch
from cmp_worker.sandbox_deploy import SandboxCluster, SandboxError, SandboxPlan, SandboxService
from cmp_worker.sandbox_isolation import RedTeamIsolation

_MIN_SECRET = 6  # shorter values are not treated as secrets (they would mask ordinary words)


def sandbox_secret_values(cluster: SandboxCluster, plan: SandboxPlan) -> list[str]:
    """Every value the Sandbox's Secrets hold, decoded, for masking and for the leak checks. They
    stay in this process: they are never logged, stored, or put into a Workflow payload."""
    try:
        raw = cluster.kubectl("get", "secrets", "-n", plan.namespace, "-o", "json")
        items = json.loads(raw or b"{}").get("items", [])
    except (SandboxError, ValueError):
        return []
    values: set[str] = set()
    for item in items:
        if item.get("type") == "kubernetes.io/service-account-token":
            continue
        for encoded in (item.get("data") or {}).values():
            try:
                text = base64.b64decode(encoded, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if len(text) >= _MIN_SECRET and text.isprintable():
                values.add(text)
    return sorted(
        values, key=len, reverse=True
    )  # longest first: a long value contains shorter ones


def build_redteam_kit(
    service: SandboxService,
    store: ArtifactStore,
    llm_factory: Callable[[str, str], LLMClient],
) -> RedTeamKit:
    """The Worker's own pieces: the real isolation, the real tools, and the model through the
    shared spend ledger (ADR-004). The approval ledger is per process: a deployment with several
    Sandbox Workers must keep it in the business database before it relies on one-use approvals."""
    return RedTeamKit(
        runner=RedTeamRunner(service, RedTeamIsolation(service.cluster), InMemoryApprovalLedger()),
        store=store,
        allowed=AllowList(set(TOOL_NAMES)),
        brain=lambda migration_id: Brain(llm_factory(migration_id, "redteam")),
        dispatch=lambda executor, secrets: make_dispatch(
            service.cluster, executor, known_secrets=secrets
        ),
        known_secrets=lambda plan: sandbox_secret_values(service.cluster, plan),
    )
