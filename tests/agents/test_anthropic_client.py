from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from cmp_agents.anthropic_client import AnthropicLLMClient, to_api_messages, to_api_tools
from cmp_agents.llm import LLMError, Message, ToolCall, ToolDefinition


class Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


def text(value):
    return Block(type="text", text=value)


def tool_use(call_id, name, **arguments):
    return Block(type="tool_use", id=call_id, name=name, input=arguments)


def thinking(value="reasoning", signature="sig"):
    return Block(type="thinking", thinking=value, signature=signature)


def reply(*content, stop="tool_use", **usage):
    fields = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    fields.update(usage)
    return SimpleNamespace(content=list(content), stop_reason=stop, usage=SimpleNamespace(**fields))


class FakeMessages:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def client(outcome, **kw):
    messages = FakeMessages(outcome)
    return AnthropicLLMClient(client=SimpleNamespace(messages=messages), **kw), messages


TOOLS = [
    ToolDefinition(
        "get_evidence",
        "Read evidence.",
        {"type": "object", "properties": {"key": {"type": "string"}}},
    )
]
USER = [Message(role="user", content="task")]


def error(cls, status=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if cls is anthropic.APIConnectionError:
        return cls(request=request)
    return cls("x", response=httpx2.Response(status, request=request), body=None)


def test_the_request_uses_sonnet_5_5_with_effort_and_none_of_the_rejected_parameters():
    c, sent = client(reply(text("ok"), stop="end_turn"))
    c.complete(system="be brief", messages=USER, tools=TOOLS)
    request = sent.requests[0]
    assert request["model"] == "claude-sonnet-5-5" and request["system"] == "be brief"
    assert request["output_config"] == {"effort": "medium"} and request["max_tokens"] == 8000
    assert request["cache_control"] == {"type": "ephemeral"}
    for rejected in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert rejected not in request
    assert request["tools"] == [
        {
            "name": "get_evidence",
            "description": "Read evidence.",
            "input_schema": TOOLS[0].parameters,
        }
    ]


def test_tool_calls_text_usage_and_cache_tokens_are_read_from_the_response():
    c, _ = client(
        reply(
            text("looking"),
            tool_use("tu-1", "get_evidence", key="a"),
            input_tokens=500,
            output_tokens=40,
            cache_read_input_tokens=2000,
            cache_creation_input_tokens=300,
        )
    )
    response = c.complete(system="", messages=USER, tools=TOOLS)
    assert response.text == "looking"
    assert response.tool_calls == [ToolCall(id="tu-1", name="get_evidence", arguments={"key": "a"})]
    assert (response.input_tokens, response.output_tokens) == (500, 40)
    assert (response.cache_read_tokens, response.cache_write_tokens) == (2000, 300)


def test_thinking_blocks_are_kept_and_sent_back_unchanged():
    c, sent = client(reply(thinking(), tool_use("tu-1", "get_evidence", key="a")))
    first = c.complete(system="", messages=USER, tools=TOOLS)
    assert first.provider_blocks[0]["type"] == "thinking"
    history = [
        *USER,
        Message(
            role="assistant",
            content="",
            tool_calls=tuple(first.tool_calls),
            provider_blocks=tuple(first.provider_blocks),
        ),
        Message(role="tool", content="evidence", tool_call_id="tu-1"),
    ]
    c.complete(system="", messages=history, tools=TOOLS)
    assistant = sent.requests[-1]["messages"][1]
    assert assistant["content"] == first.provider_blocks  # byte for byte what came back


def test_without_provider_blocks_an_assistant_turn_is_rebuilt_from_text_and_calls():
    history = [
        *USER,
        Message(
            role="assistant",
            content="hmm",
            tool_calls=(ToolCall("t1", "get_evidence", {"key": "k"}),),
        ),
        Message(role="tool", content="r1", tool_call_id="t1"),
    ]
    api = to_api_messages(history)
    assert api[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "hmm"},
            {"type": "tool_use", "id": "t1", "name": "get_evidence", "input": {"key": "k"}},
        ],
    }
    assert api[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r1"}],
    }


def test_results_for_several_calls_and_a_following_nudge_share_one_user_message():
    history = [
        Message(role="user", content="task"),
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall("a", "x", {}), ToolCall("b", "y", {})),
        ),
        Message(role="tool", content="ra", tool_call_id="a"),
        Message(role="tool", content="rb", tool_call_id="b"),
        Message(role="user", content="Call a tool, or submit."),
    ]
    api = to_api_messages(history)
    assert [m["role"] for m in api] == ["user", "assistant", "user"]
    assert [b["type"] for b in api[2]["content"]] == ["tool_result", "tool_result", "text"]


def test_no_tools_and_no_system_leave_those_fields_out():
    c, sent = client(reply(text("hi"), stop="end_turn"))
    c.complete(system="", messages=USER, tools=[])
    assert "tools" not in sent.requests[0] and "system" not in sent.requests[0]
    assert to_api_tools([]) == []


@pytest.mark.parametrize(
    ("stop", "code"), [("refusal", "LLM_REFUSED"), ("max_tokens", "LLM_OUTPUT_TRUNCATED")]
)
def test_a_refusal_or_a_cut_off_answer_ends_the_call_with_a_code(stop, code):
    c, _ = client(reply(text("partial"), stop=stop))
    with pytest.raises(LLMError) as exc:
        c.complete(system="", messages=USER, tools=TOOLS)
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: error(anthropic.AuthenticationError, 401), "LLM_AUTH"),
        (lambda: error(anthropic.PermissionDeniedError, 403), "LLM_AUTH"),
        (lambda: error(anthropic.BadRequestError, 400), "LLM_BAD_REQUEST"),
        (lambda: error(anthropic.NotFoundError, 404), "LLM_BAD_REQUEST"),
        (lambda: error(anthropic.RateLimitError, 429), "LLM_UNAVAILABLE"),
        (lambda: error(anthropic.InternalServerError, 500), "LLM_UNAVAILABLE"),
        (lambda: error(anthropic.APIConnectionError), "LLM_UNAVAILABLE"),
    ],
)
def test_provider_errors_become_stable_codes_without_the_provider_message(make, code):
    c, _ = client(make())
    with pytest.raises(LLMError) as exc:
        c.complete(system="", messages=USER, tools=TOOLS)
    assert exc.value.code == code and exc.value.__cause__ is None
