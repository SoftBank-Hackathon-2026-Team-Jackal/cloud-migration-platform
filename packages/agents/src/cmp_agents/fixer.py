"""Fixer: proposes a requirements/intent fix (SPEC) or an application code fix (CODE, BUILD).

The Fixer never decides whether to run: `propose_fix` refuses INFRA and RENDER before any model
call. A fix is data; applying it is mechanical code here, and tests are never writable.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from cmp_contracts.provisioning_v2 import NetworkIntent, RequirementsV2
from cmp_scanners.masking import mask_text
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cmp_agents.llm import LLMClient
from cmp_agents.prompt_store import load_prompt
from cmp_agents.runtime import AgentRecord, Budget, run_agent
from cmp_agents.tools import ToolRegistry, ToolSpec

_MAX_FILE_BYTES = 200_000
_MAX_FILES = 20
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|testdata|spec|specs|e2e)(/|$)"
    r"|_test\.(go|py)$|(^|/)test_[^/]*\.py$|\.(test|spec)\.[a-z]+$|(^|/)conftest\.py$",
    re.IGNORECASE,
)
_ENV_PATH = re.compile(r"(^|/)\.env([./].*)?$")


class FixerError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceFieldEdit(_Strict):
    resource_id: str
    field: Literal["port", "health_path", "replicas", "cpu_millicores", "memory_mib", "storage_gib"]
    value: int | str


class RowChange(_Strict):
    """A mapping row now handled differently (kept at the source or migrated). It never touches
    `grade` or `required`: those stay with the analysis and the person who confirmed them."""

    row_id: str
    disposition: Literal["MIGRATE", "KEEP"]


class RequirementsFix(_Strict):
    reason: str = Field(min_length=1)
    edits: list[ResourceFieldEdit] = Field(default_factory=list)
    row_changes: list[RowChange] = Field(default_factory=list)
    add_intents: list[NetworkIntent] = Field(default_factory=list)
    remove_intents: list[NetworkIntent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _changes_something(self):
        if not (self.edits or self.add_intents or self.remove_intents or self.row_changes):
            raise ValueError("a fix must change something")
        return self


class FileChange(_Strict):
    path: str
    content: str


def is_protected_path(path: str) -> str | None:
    """Why a path may not be written, or None when it may."""
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or not path or path.startswith("~"):
        return "PATH_OUTSIDE_WORKSPACE"
    if ".git" in posix.parts:
        return "PATH_PROTECTED"
    if _ENV_PATH.search(path):
        return "ENV_FILE_REFUSED"
    if _TEST_PATH.search(path):
        return "TEST_FILE_CHANGE_REFUSED"
    return None


class CodeFix(_Strict):
    reason: str = Field(min_length=1)
    files: list[FileChange] = Field(min_length=1, max_length=_MAX_FILES)

    @field_validator("files")
    @classmethod
    def _writable(cls, files: list[FileChange]) -> list[FileChange]:
        for change in files:
            problem = is_protected_path(change.path)
            if problem:
                raise ValueError(f"{problem}: {change.path}")
            if len(change.content.encode()) > _MAX_FILE_BYTES:
                raise ValueError(f"FILE_TOO_LARGE: {change.path}")
        return files


def apply_requirements_fix(requirements: RequirementsV2, fix: RequirementsFix) -> RequirementsV2:
    """Mechanically apply the edits; the result must still be valid requirements."""
    data = requirements.model_dump(mode="json")
    by_id = {r["id"]: r for r in data["resources"]}
    for edit in fix.edits:
        resource = by_id.get(edit.resource_id)
        if resource is None:
            raise FixerError("UNKNOWN_RESOURCE")
        if edit.field not in resource:
            raise FixerError("FIELD_NOT_ON_RESOURCE")
        resource[edit.field] = edit.value
    intents = data["network_intent"]
    for gone in (i.model_dump(mode="json") for i in fix.remove_intents):
        if gone not in intents:
            raise FixerError("INTENT_NOT_FOUND")
        intents.remove(gone)
    for new in (i.model_dump(mode="json") for i in fix.add_intents):
        if new not in intents:
            intents.append(new)
    try:
        return RequirementsV2.model_validate(data)
    except ValidationError:
        raise FixerError("FIX_MAKES_REQUIREMENTS_INVALID") from None


def apply_code_fix(workspace: Path | str, fix: CodeFix) -> list[str]:
    """Write the files into the work space; paths are re-checked at write time."""
    root = Path(workspace).resolve()
    written: list[str] = []
    for change in fix.files:
        problem = is_protected_path(change.path)
        if problem:
            raise FixerError(problem)
        target = (root / change.path).resolve()
        if root not in target.parents:
            raise FixerError("PATH_OUTSIDE_WORKSPACE")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.content)
        written.append(change.path)
    return sorted(written)


@dataclass
class FixInput:
    failure_class: str
    reasons: list[str]
    requirements_json: str = ""
    logs: str = ""  # application and proxy logs; masked again here
    tests: dict[str, str] = field(default_factory=dict)  # read-only test definitions by id


@dataclass(frozen=True)
class FixOutcome:
    status: str  # PROPOSED | NOT_CALLED | FAILED
    requirements_fix: RequirementsFix | None = None
    code_fix: CodeFix | None = None
    reason: str | None = None
    record: AgentRecord | None = None


class _Path(BaseModel):
    path: str


class _TestId(BaseModel):
    case_id: str


def _task(data: FixInput) -> str:
    return "\n".join(
        [
            f"## Failure class: {data.failure_class}",
            *[f"- {r}" for r in data.reasons],
            "",
            "## Requirements",
            mask_text(data.requirements_json) or "(not provided)",
            "",
            "## Logs (masked)",
            mask_text(data.logs)[-6000:] or "(none)",
            "",
            "## Tests (read only)",
            *[f"- {case_id}" for case_id in sorted(data.tests)],
        ]
    )


def propose_fix(
    data: FixInput,
    *,
    llm: LLMClient,
    workspace: Path | str | None = None,
    budget: Budget | None = None,
    max_attempts: int = 2,
) -> FixOutcome:
    if data.failure_class in {"INFRA", "RENDER"}:
        return FixOutcome("NOT_CALLED", reason=f"{data.failure_class}_IS_NOT_FIXABLE")
    wants_spec = data.failure_class == "SPEC"
    if not wants_spec and workspace is None:
        return FixOutcome("NOT_CALLED", reason="WORKSPACE_REQUIRED")
    root = Path(workspace).resolve() if workspace else None

    def read_file(args: _Path) -> str:
        if root is None or is_protected_path(args.path) == "PATH_OUTSIDE_WORKSPACE":
            return "error: path is outside the workspace"
        if _ENV_PATH.search(args.path):
            return "error: environment files are not readable"
        target = (root / args.path).resolve()
        if root not in target.parents or not target.is_file():
            return f"error: no such file '{args.path}'"
        return mask_text(target.read_text(errors="ignore")[:_MAX_FILE_BYTES])

    def read_test(args: _TestId) -> str:
        return (
            json.dumps(data.tests[args.case_id])
            if args.case_id in data.tests
            else "error: unknown test"
        )

    tools = ToolRegistry()
    tools.register(
        ToolSpec("read_test", "Read one test definition (read only).", _TestId, read_test)
    )
    if not wants_spec:
        tools.register(ToolSpec("read_file", "Read one file of the workspace.", _Path, read_file))
    prompt, version = load_prompt("fixer")
    submit, model = (
        ("submit_requirements_fix", RequirementsFix) if wants_spec else ("submit_code_fix", CodeFix)
    )
    last = None
    for _ in range(max_attempts):
        last = run_agent(
            role="fixer", task=_task(data), llm=llm, tools=tools, submit_name=submit,
            result_model=model, budget=budget or Budget(), prompt=prompt, prompt_version=version,
        )  # fmt: skip
        if last.status == "SUBMITTED":
            if wants_spec:
                return FixOutcome("PROPOSED", requirements_fix=last.result, record=last.record)
            return FixOutcome("PROPOSED", code_fix=last.result, record=last.record)
        if last.fatal:
            break
    return FixOutcome("FAILED", reason=last.failure_reason, record=last.record)
