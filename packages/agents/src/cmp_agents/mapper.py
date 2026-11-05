"""Mapper: proposes mapping rows from masked evidence; everything else is mechanical (AV-007)."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cmp_contracts.analysis import Evidence, MappingRow
from cmp_scanners.masking import mask_text
from pydantic import BaseModel, ConfigDict, Field

from cmp_agents.catalog import Catalog
from cmp_agents.llm import LLMClient
from cmp_agents.mapping import apply_dispositions, stabilize_row_ids
from cmp_agents.prompt_store import load_prompt
from cmp_agents.runtime import AgentRecord, Budget, run_agent
from cmp_agents.skills import SkillLoader, make_load_skill_tool
from cmp_agents.tools import ToolRegistry, ToolSpec


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceProposal(_Strict):
    kind: Literal["CODE", "MANIFEST", "CLOUD_RESOURCE", "IAM_POLICY", "CLOUDTRAIL", "CONFIG"]
    locator: str


class MappingProposal(_Strict):
    id: str
    capability: str
    usage: str
    grade: Literal["DIRECT", "API_DIFF", "PARTIAL", "NONE"]
    required_proposed: bool
    required_reason: str = Field(min_length=1)
    disposition: Literal["MIGRATE", "KEEP"]
    loses: list[str]
    evidence: list[EvidenceProposal] = Field(min_length=1)


class MappingSubmission(_Strict):
    rows: list[MappingProposal]


@dataclass
class MapperInput:
    evidence_summary: str
    evidence_items: dict[str, str] = field(default_factory=dict)
    previous_rows: list[MappingRow] = field(default_factory=list)


@dataclass(frozen=True)
class MapResult:
    status: str  # MAPPED | NEEDS_REVIEW
    rows: list[MappingRow]
    attempts: int
    failure_reason: str | None
    record: AgentRecord | None


class _Key(BaseModel):
    key: str


class _Capability(BaseModel):
    capability: str


def _task(summary: str, candidates, previous: list[MappingRow]) -> str:
    lines = ["## Evidence summary", summary, "", "## Catalog candidates"]
    for entry in candidates:
        lines.append(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False))
    lines += ["", "## Previous rows (keep these ids for the same usage)"]
    lines += [f"{r.id} | {r.capability} | {r.usage}" for r in previous] or ["(none)"]
    return "\n".join(lines)


def map_usage(
    data: MapperInput,
    *,
    llm: LLMClient,
    catalog: Catalog,
    skills_root: Path | str,
    budget: Budget | None = None,
    max_turns: int | None = None,
    max_attempts: int = 3,
) -> MapResult:
    summary = mask_text(data.evidence_summary)
    items = {key: mask_text(text) for key, text in data.evidence_items.items()}
    candidates = catalog.candidates("\n".join([summary, *items.values()]))
    loader = SkillLoader(skills_root, allowed=[c.skill for c in candidates if c.skill])

    def get_evidence(args: _Key) -> str:
        return items.get(args.key, f"error: unknown evidence key '{args.key}'")

    def get_entry(args: _Capability) -> str:
        try:
            return json.dumps(
                catalog.get(args.capability).model_dump(mode="json"), ensure_ascii=False
            )
        except KeyError:
            return f"error: '{args.capability}' is not in the catalog"

    tools = ToolRegistry()
    tools.register(ToolSpec("get_evidence", "Read one masked evidence item.", _Key, get_evidence))
    tools.register(ToolSpec("get_catalog_entry", "Read one catalog entry.", _Capability, get_entry))
    tools.register(make_load_skill_tool(loader))

    prompt, version = load_prompt("mapper")
    base = budget or Budget()
    run_budget = Budget(
        max_turns=max_turns or base.max_turns,
        max_input_tokens=base.max_input_tokens,
        max_output_tokens=base.max_output_tokens,
        max_seconds=base.max_seconds,
    )
    task = _task(summary, candidates, data.previous_rows)
    last = None
    for attempt in range(1, max_attempts + 1):
        last = run_agent(
            role="mapper",
            task=task,
            llm=llm,
            tools=tools,
            submit_name="submit_mapping",
            result_model=MappingSubmission,
            budget=run_budget,
            skills=loader,
            prompt=prompt,
            prompt_version=version,
        )
        if last.status == "SUBMITTED":
            rows = [
                MappingRow(
                    id=p.id,
                    capability=p.capability,
                    usage=p.usage,
                    grade=p.grade,
                    required_proposed=p.required_proposed,
                    required_reason=p.required_reason,
                    required=None,
                    disposition=p.disposition,
                    loses=p.loses,
                    treatment=None,
                    status="UNVERIFIED",
                    evidence=[
                        Evidence(kind=e.kind, locator=e.locator, verified=False) for e in p.evidence
                    ],
                )
                for p in last.result.rows
            ]
            rows = apply_dispositions(stabilize_row_ids(rows, data.previous_rows))
            return MapResult("MAPPED", rows, attempt, None, last.record)
        if last.fatal:
            return MapResult("NEEDS_REVIEW", [], attempt, last.failure_reason, last.record)
    return MapResult("NEEDS_REVIEW", [], max_attempts, last.failure_reason, last.record)
