"""Ticket 35: the red-team Agent is a decision maker with a fixed, code-enforced Tool boundary.

Part 1 (this block): the call schema and the allow list. A call never carries a host, URL or IP —
only a tool name and a path inside the already-fixed target app — so nothing the model outputs
can send the executor anywhere but the one Service the isolation session already bound it to.
"""

import pytest
from cmp_agents.redteam_agent import OUT_OF_SCOPE_TARGET, AllowList, ToolCall
from pydantic import ValidationError


def test_a_call_names_a_tool_and_a_path_only():
    call = ToolCall(tool="probe_get", path="/orders/1")
    assert call.tool == "probe_get" and call.path == "/orders/1"


@pytest.mark.parametrize(
    "path",
    [
        "http://example.com/x", "https://10.0.0.1/x", "//example.com/x", "example.com/x",
        "10.0.0.1", "169.254.169.254/latest/meta-data/", "ftp://host/x", "",
    ],
)  # fmt: skip
def test_a_path_that_names_a_host_is_rejected_by_the_schema_itself(path):
    with pytest.raises(ValidationError):
        ToolCall(tool="probe_get", path=path)


def test_the_schema_has_no_field_that_could_carry_a_host_url_or_ip():
    fields = set(ToolCall.model_fields)
    assert fields == {"tool", "path"}
    for banned in ("host", "url", "ip", "address", "endpoint", "base_url", "target"):
        assert banned not in fields


def test_an_unknown_field_is_rejected_not_silently_dropped():
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"tool": "probe_get", "path": "/x", "host": "evil.example"})


# --- the allow list -------------------------------------------------------------------------------


def test_only_registered_tool_names_are_accepted():
    allowed = AllowList({"probe_get", "probe_post"})
    assert allowed.check(ToolCall(tool="probe_get", path="/x")) is None
    assert allowed.check(ToolCall(tool="run_shell", path="/x")) == OUT_OF_SCOPE_TARGET


def test_the_allow_list_itself_refuses_a_shell_or_network_looking_name():
    for name in ("shell", "run_command", "curl", "http_get", "kubectl_exec"):
        with pytest.raises(ValueError):
            AllowList({name})


def test_the_allow_list_is_empty_by_default_so_nothing_is_accepted_without_being_named():
    allowed = AllowList()
    assert allowed.check(ToolCall(tool="probe_get", path="/x")) == OUT_OF_SCOPE_TARGET
