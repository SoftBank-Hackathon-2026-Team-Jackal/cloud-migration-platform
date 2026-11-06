"""Tester: proposes tests from the API list, usage summary and the source it may read (AV-012).

Its input type has no field for Fixer output, so a Fixer change cannot reach this session.
Proposals are not trusted: `cmp_testsuite.validate_tests` decides which ones count.

A model does not write expected values (ticket 27-B). It proposes a write `shape` (how the app
takes a write and gives it back) and read-only `probes` (requests to be answered), each with the
source lines it comes from. The platform builds the tests from the shape and records the answers
of the probes. A free-form `case` with its own expected values is only taken when an origin test
resource can check it.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from cmp_scanners.masking import is_env_file, mask_text
from cmp_scanners.routes import HttpRoute
from cmp_testsuite.evidence import read_source_lines
from cmp_testsuite.model import SuiteCase
from cmp_testsuite.property import ShapeProposal, property_cases
from cmp_testsuite.record import Probe
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cmp_agents.llm import LLMClient
from cmp_agents.prompt_store import load_prompt
from cmp_agents.runtime import AgentRecord, Budget, run_agent
from cmp_agents.skills import SkillLoader, make_load_skill_tool
from cmp_agents.tools import ToolRegistry, ToolSpec

_SNIPPET_LINES = 60
_SECRET_FILE = ("id_rsa", "id_ed25519", "credentials", ".pem", ".key", ".p12", ".pfx", ".tfstate")
SKILL_NAME = "test-authoring"


class TestsSubmission(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="forbid")

    cases: list[SuiteCase] = Field(default_factory=list)
    shapes: list[ShapeProposal] = Field(default_factory=list)
    probes: list[Probe] = Field(default_factory=list)

    @field_validator("cases")
    @classmethod
    def _only_ai(cls, cases: list[SuiteCase]) -> list[SuiteCase]:
        if any(c.source != "AI" for c in cases):
            raise ValueError("submitted tests must have source AI")
        if any(c.basis != "FREE" for c in cases):
            raise ValueError("only the platform can mark expected values as recorded")
        return cases

    @model_validator(mode="after")
    def _something_to_work_with(self) -> "TestsSubmission":
        if not (self.cases or self.shapes or self.probes):
            raise ValueError("submit at least one shape, probe or case")
        return self


class _NoFreeCases(TestsSubmission):
    @field_validator("cases")
    @classmethod
    def _no_free_cases(cls, cases: list[SuiteCase]) -> list[SuiteCase]:
        if cases:
            raise ValueError(
                "free-form cases are not accepted here (nothing can vouch for their expected "
                "values); submit `shapes` and `probes` instead"
            )
        return cases


@dataclass
class SuiteRequest:
    usage_summary: str
    api_routes: dict[str, str] = field(default_factory=dict)  # "METHOD /path" -> description
    existing_ids: list[str] = field(default_factory=list)
    # Ticket 27-B: what the Tester may read. Nothing here carries a Fixer change.
    http_routes: list[HttpRoute] = field(default_factory=list)
    workspace: Path | None = None  # the fetched source; without it there is no snippet tool
    skills_root: Path | None = None  # where `test-authoring` lives; without it, no skill tool
    origin_available: bool = True  # False: free-form cases would be thrown away, so none are taken


@dataclass(frozen=True)
class GenerateResult:
    status: str  # GENERATED | FAILED
    cases: list[SuiteCase]
    failure_reason: str | None
    record: AgentRecord | None
    probes: tuple[Probe, ...] = ()


class _Route(BaseModel):
    route: str


class _Snippet(BaseModel):
    file: str
    line: int = Field(ge=1)
    count: int = Field(default=30, ge=1)


def _snippet(workspace: Path, args: _Snippet) -> str:
    name = Path(args.file).name.lower()
    if is_env_file(Path(args.file).name) or any(part in name for part in _SECRET_FILE):
        return "error: this file is not available"
    lines = read_source_lines(workspace, args.file)
    if lines is None:
        return "error: no such file in the source"
    start = args.line - 1
    chosen = lines[start : start + min(args.count, _SNIPPET_LINES)]
    if not chosen:
        return "error: line is past the end of the file"
    return mask_text("\n".join(f"{start + i + 1}: {text}" for i, text in enumerate(chosen)))


def generate_tests(
    data: SuiteRequest, *, llm: LLMClient, budget: Budget | None = None, max_attempts: int = 2
) -> GenerateResult:
    routes = {key: mask_text(text) for key, text in data.api_routes.items()}

    def get_api(args: _Route) -> str:
        return routes.get(args.route, f"error: unknown route '{args.route}'")

    tools = ToolRegistry()
    tools.register(ToolSpec("get_api", "Read one route's description.", _Route, get_api))
    if data.workspace is not None:
        workspace = Path(data.workspace)
        tools.register(
            ToolSpec(
                "get_snippet",
                "Read a few numbered lines of the application source (secrets masked).",
                _Snippet,
                lambda args: _snippet(workspace, args),
            )
        )
    skills = None
    if data.skills_root is not None:
        skills = SkillLoader(data.skills_root, allowed=[SKILL_NAME])
        tools.register(make_load_skill_tool(skills))
    prompt, version = load_prompt("tester")
    found = [
        f"- {r.method or 'ANY'} {r.path} ({r.file}:{r.line})"
        for r in sorted(data.http_routes, key=lambda r: (r.path, r.method or "", r.file, r.line))
    ]
    task = "\n".join(
        [
            "## Usage summary",
            mask_text(data.usage_summary),
            "",
            "## API routes",
            *[f"- {key}" for key in routes],
            "",
            "## Routes found in the source",
            *(found or ["(none)"]),
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
            result_model=TestsSubmission if data.origin_available else _NoFreeCases,
            budget=budget or Budget(),
            skills=skills,
            prompt=prompt,
            prompt_version=version,
        )
        if last.status == "SUBMITTED":
            built = [c for shape in last.result.shapes for c in property_cases(shape)]
            cases = [*last.result.cases, *built]
            return GenerateResult("GENERATED", cases, None, last.record, tuple(last.result.probes))
        if last.fatal:
            break
    return GenerateResult("FAILED", [], last.failure_reason, last.record)
