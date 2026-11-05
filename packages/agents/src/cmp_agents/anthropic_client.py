"""Claude through the Anthropic SDK (ADR-004), behind the `LLMClient` interface.

Sonnet 5.5 specifics: thinking is always on, so no `thinking`, `temperature` or forced
`tool_choice` is sent (those are rejected); depth is steered with `effort`. Thinking blocks are
returned in `provider_blocks` and sent back unchanged on the next turn. No fallback model is
configured, so a refusal ends the run instead of being re-run (and billed) on another model.
The API key is read by the SDK from the environment; this module never sees or stores it.
"""

from collections.abc import Sequence
from typing import Any

import anthropic

from cmp_agents.llm import LLMError, LLMResponse, Message, ToolCall, ToolDefinition

DEFAULT_MODEL = "claude-sonnet-5-5"


def to_api_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Our messages to API messages. Tool results ride in a user message, merged when they
    follow each other, because the API wants every `tool_use` answered in the next user turn."""
    out: list[dict[str, Any]] = []

    def user_blocks() -> list[dict[str, Any]]:
        if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
            return out[-1]["content"]
        out.append({"role": "user", "content": []})
        return out[-1]["content"]

    for message in messages:
        if message.role == "tool":
            user_blocks().append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            )
        elif message.role == "user":
            user_blocks().append({"type": "text", "text": message.content})
        else:
            if message.provider_blocks is not None:
                content = list(message.provider_blocks)
            else:
                content = [{"type": "text", "text": message.content}] if message.content else []
                content += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in message.tool_calls
                ]
            out.append({"role": "assistant", "content": content})
    return out


def to_api_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
    ]


class AnthropicLLMClient:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8000,
        effort: str = "medium",
        client: Any | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.model, self.max_tokens, self.effort = model, max_tokens, effort
        self._client = client or anthropic.Anthropic(timeout=timeout)

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},  # caches the stable prefix of long loops
            "messages": to_api_messages(messages),
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = to_api_tools(tools)
        try:
            response = self._client.messages.create(**request)
        except anthropic.AuthenticationError:
            raise LLMError("LLM_AUTH") from None
        except anthropic.PermissionDeniedError:
            raise LLMError("LLM_AUTH") from None
        except (anthropic.BadRequestError, anthropic.NotFoundError):
            raise LLMError("LLM_BAD_REQUEST") from None
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError):
            # The SDK already retried; give up with a code instead of spinning Activity retries.
            raise LLMError("LLM_UNAVAILABLE") from None
        if response.stop_reason == "refusal":
            raise LLMError("LLM_REFUSED")
        if response.stop_reason == "max_tokens":
            raise LLMError("LLM_OUTPUT_TRUNCATED")  # a cut-off tool call cannot be trusted
        text = "".join(b.text for b in response.content if b.type == "text") or None
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        usage = response.usage
        return LLMResponse(
            tool_calls=calls,
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            provider_blocks=[b.model_dump(exclude_none=True) for b in response.content],
        )
