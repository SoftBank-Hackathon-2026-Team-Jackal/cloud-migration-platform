"""Tester: proposes HTTP scenarios from the API list and usage summary only (AV-012).

Its input type has no field for Fixer output, so a Fixer change cannot reach this session.
Proposals are not trusted: `cmp_testsuite.validate_tests` decides which ones count.
"""

import json
from dataclasses import dataclass, field

from cmp_scanners.masking import mask_text
from cmp_testsuite.model import SuiteCase
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cmp_agents.llm import LLMClient
from cmp_agents.prompt_store import load_prompt
from cmp_agents.runtime import AgentRecord, Budget, run_agent
from cmp_agents.tools import ToolRegistry, ToolSpec


class TestsSubmission(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="forbid")

    cases: list[SuiteCase] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _only_ai(cls, cases: list[SuiteCase]) -> list[SuiteCase]:
        if any(c.source != "AI" for c in cases):
            raise ValueError("submitted tests must have source AI")
        return cases


@dataclass
class SuiteRequest:
    usage_summary: str
    api_routes: dict[str, str] = field(default_factory=dict)  # "METHOD /path" -> description
    existing_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GenerateResult:
    status: str  # GENERATED | FAILED
    cases: list[SuiteCase]
    failure_reason: str | None
    record: AgentRecord | None


class _Route(BaseModel):
    route: str


def generate_tests(
    data: SuiteRequest, *, llm: LLMClient, budget: Budget | None = None, max_attempts: int = 2
) -> GenerateResult:
    routes = {key: mask_text(text) for key, text in data.api_routes.items()}

    def get_api(args: _Route) -> str:
        return routes.get(args.route, f"error: unknown route '{args.route}'")

    tools = ToolRegistry()
    tools.register(ToolSpec("get_api", "Read one route's description.", _Route, get_api))
    prompt, version = load_prompt("tester")
    task = "\n".join(
        [
            "## Usage summary",
            mask_text(data.usage_summary),
            "",
            "## API routes",
            *[f"- {key}" for key in routes],
            "",
            "## Already present test ids (do not repeat)",
            json.dumps(sorted(data.existing_ids)),
        ]
    )
    last = None
    for _ in range(max_attempts):
        last = run_agent(
            role="tester",
            task=task,
            llm=llm,
            tools=tools,
            submit_name="submit_tests",
            result_model=TestsSubmission,
            budget=budget or Budget(),
            prompt=prompt,
            prompt_version=version,
        )
        if last.status == "SUBMITTED":
            return GenerateResult("GENERATED", list(last.result.cases), None, last.record)
        if last.fatal:
            break
    return GenerateResult("FAILED", [], last.failure_reason, last.record)
