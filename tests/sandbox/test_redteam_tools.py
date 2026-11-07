"""Ticket 38: the fixed tools an executor runs. A fake cluster records exactly what would be run."""

import json

import pytest
from cmp_agents.redteam_agent import AllowList, ToolCall
from cmp_worker.redteam_tools import PROBE_GET, TOOL_NAMES, make_dispatch
from cmp_worker.sandbox_deploy import SandboxError
from cmp_worker.sandbox_isolation import Executor

EXECUTOR = Executor(name="cmp-redteam-x", namespace="ns", target_ip="10.43.0.40", port=8080)


class Cluster:
    def __init__(self, output=b'{"status": 200, "headers": {}, "body": "ok"}', fails=False):
        self.output, self.fails, self.calls = output, fails, []

    def kubectl(self, *args, timeout=300):
        self.calls.append(args)
        if self.fails:
            raise SandboxError("CLUSTER_COMMAND_FAILED")
        return self.output


def test_the_tool_names_pass_the_allow_list_rule_that_forbids_shell_and_network_words():
    AllowList(set(TOOL_NAMES))  # raises if a name reads like a shell or network escape


def test_a_call_runs_a_constant_program_with_only_argv_varying_and_no_shell():
    cluster = Cluster()
    dispatch = make_dispatch(cluster, EXECUTOR)
    dispatch(ToolCall(tool=PROBE_GET, path="/items?id=2"))
    (args,) = cluster.calls
    assert args[:5] == ("exec", "-n", "ns", "cmp-redteam-x", "--")
    assert args[5:7] == ("python", "-c")
    assert args[-3:] == ("10.43.0.40", "8080", "/items?id=2")  # the bound address, not the model's
    for shell in ("sh", "bash", "-s"):
        assert shell not in args[5:7]
    assert "/items" not in args[7]  # the program text does not contain the path
    again = Cluster()
    make_dispatch(again, EXECUTOR)(ToolCall(tool=PROBE_GET, path="/other"))
    assert again.calls[0][7] == args[7]  # the same program every time


def test_a_path_cannot_carry_a_destination_even_if_it_looks_like_one():
    for bad in ("http://evil/x", "//evil/x", "/10.0.0.9/x"):
        with pytest.raises(ValueError):
            ToolCall(tool=PROBE_GET, path=bad)


def test_an_unknown_tool_is_refused_without_running_anything():
    cluster = Cluster()
    result = make_dispatch(cluster, EXECUTOR)(ToolCall(tool="probe_post", path="/x"))
    assert cluster.calls == [] and result.text == "OUT_OF_SCOPE_TARGET"


def test_the_result_is_masked_with_the_apps_own_secret_values():
    secret = "Zx9-live-key-4471"
    body = json.dumps({"status": 200, "headers": {}, "body": f"key={secret}"}).encode()
    result = make_dispatch(Cluster(body), EXECUTOR, known_secrets=[secret])(
        ToolCall(tool=PROBE_GET, path="/debug")
    )
    assert secret not in result.text and '"status": 200' in result.text


def test_unreadable_tool_output_is_reported_as_data_not_trusted():
    result = make_dispatch(Cluster(b"Traceback: boom"), EXECUTOR)(
        ToolCall(tool=PROBE_GET, path="/x")
    )
    assert result.text == "TOOL_OUTPUT_UNREADABLE"


def test_a_pod_that_cannot_be_reached_raises_so_the_loop_stops_instead_of_guessing():
    with pytest.raises(SandboxError):
        make_dispatch(Cluster(fails=True), EXECUTOR)(ToolCall(tool=PROBE_GET, path="/x"))
