"""LLM client interface and a scripted fake for tests (provider choice is D-01)."""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

_ids = itertools.count(1)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    tool_calls: Sequence[ToolCall] = ()
    text: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse: ...


def call(tool_name: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call-{next(_ids)}", name=tool_name, arguments=arguments)


@dataclass
class FakeCall:
    system: str
    messages: list[Message]
    tools: list[ToolDefinition]


@dataclass
class FakeLLMClient:
    """Replays fixed responses in order and records what it was asked."""

    responses: Sequence[LLMResponse]
    calls: list[FakeCall] = field(default_factory=list)
    _position: int = 0

    def complete(self, *, system, messages, tools) -> LLMResponse:
        self.calls.append(FakeCall(system, list(messages), list(tools)))
        if self._position >= len(self.responses):
            raise RuntimeError("FakeLLMClient script exhausted")
        response = self.responses[self._position]
        self._position += 1
        return response
