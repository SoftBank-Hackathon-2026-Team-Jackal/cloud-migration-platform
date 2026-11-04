"""Tool-calling loop with a schema-enforced submit tool and run budgets (plan 2.11)."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from cmp_agents.llm import LLMClient, Message, ToolCall, ToolDefinition
from cmp_agents.skills import SkillLoader
from cmp_agents.tools import ToolRegistry

_SUMMARY_LIMIT = 300


@dataclass(frozen=True)
class Budget:
    max_turns: int = 20
    max_input_tokens: int = 200_000
    max_output_tokens: int = 50_000
    max_seconds: float = 300.0


class Step(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn: int
    tool: str
    arguments_summary: str
    result_summary: str
    ok: bool


class AgentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    prompt_version: str | None
    skills_loaded: list[str]
    steps: list[Step]
    turns: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class AgentRun:
    status: str  # SUBMITTED | FAILED
    result: BaseModel | None
    failure_reason: str | None
    record: AgentRecord


def _summary(value: Any) -> str:
    # Imported here so the runtime stays usable where scanners are not installed.
    from cmp_scanners.masking import mask_text

    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return mask_text(text)[:_SUMMARY_LIMIT]


def run_agent(
    *,
    role: str,
    task: str,
    llm: LLMClient,
    tools: ToolRegistry,
    submit_name: str,
    result_model: type[BaseModel],
    budget: Budget,
    skills: SkillLoader | None = None,
    prompt: str = "",
    prompt_version: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AgentRun:
    definitions = [
        ToolDefinition(t.name, t.description, t.input_model.model_json_schema())
        for t in tools.specs()
    ]
    definitions.append(
        ToolDefinition(submit_name, "Submit the final result.", result_model.model_json_schema())
    )
    messages = [Message(role="user", content=task)]
    steps: list[Step] = []
    started = clock()
    skills_before = len(skills.loaded) if skills else 0
    state = {"turns": 0, "input": 0, "output": 0}

    def finish(status, result=None, reason=None) -> AgentRun:
        record = AgentRecord(
            role=role,
            prompt_version=prompt_version,
            skills_loaded=list(skills.loaded[skills_before:]) if skills else [],
            steps=steps,
            turns=state["turns"],
            input_tokens=state["input"],
            output_tokens=state["output"],
        )
        return AgentRun(status=status, result=result, failure_reason=reason, record=record)

    def record_step(call: ToolCall, result: str, ok: bool) -> None:
        steps.append(
            Step(
                turn=state["turns"],
                tool=call.name,
                arguments_summary=_summary(call.arguments),
                result_summary=_summary(result),
                ok=ok,
            )
        )
        messages.append(Message(role="tool", content=result, tool_call_id=call.id))

    while True:
        if state["turns"] >= budget.max_turns:
            return finish("FAILED", reason="BUDGET_TURNS")
        if clock() - started > budget.max_seconds:
            return finish("FAILED", reason="BUDGET_TIME")
        state["turns"] += 1
        response = llm.complete(system=prompt, messages=messages, tools=definitions)
        state["input"] += response.input_tokens
        state["output"] += response.output_tokens
        if state["input"] > budget.max_input_tokens:
            return finish("FAILED", reason="BUDGET_INPUT_TOKENS")
        if state["output"] > budget.max_output_tokens:
            return finish("FAILED", reason="BUDGET_OUTPUT_TOKENS")
        if clock() - started > budget.max_seconds:
            return finish("FAILED", reason="BUDGET_TIME")

        calls = list(response.tool_calls)
        messages.append(
            Message(role="assistant", content=response.text or "", tool_calls=tuple(calls))
        )
        if not calls:
            messages.append(
                Message(
                    role="user",
                    content=f"Call a tool, or call {submit_name} with the final result.",
                )
            )
            continue
        for tool_call in calls:
            if tool_call.name == submit_name:
                try:
                    result = result_model.model_validate(tool_call.arguments)
                except ValidationError as exc:
                    record_step(tool_call, f"invalid submission: {exc}", ok=False)
                    continue
                record_step(tool_call, "accepted", ok=True)
                return finish("SUBMITTED", result=result)
            spec = tools.get(tool_call.name)
            if spec is None:
                record_step(tool_call, f"error: tool '{tool_call.name}' is not allowed", ok=False)
                continue
            try:
                arguments = spec.input_model.model_validate(tool_call.arguments)
            except ValidationError as exc:
                record_step(tool_call, f"invalid arguments: {exc}", ok=False)
                continue
            try:
                output = spec.handler(arguments)
            except Exception as exc:  # noqa: BLE001 - tool failures go back to the model
                record_step(tool_call, f"error: {type(exc).__name__}", ok=False)
                continue
            record_step(
                tool_call,
                output if isinstance(output, str) else json.dumps(output, default=str),
                ok=True,
            )
