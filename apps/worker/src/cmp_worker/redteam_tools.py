"""The fixed tools a red-team executor Pod runs (ticket 38, ADR-008 decisions 2, 5 and 8).

A tool is a constant program in this file. The only things that vary per call are three argv
values: the address and port the isolation session bound (not chosen by the model) and a path that
`ToolCall` already proved stays inside the app. Nothing is interpolated into the program text and no
shell is involved (`kubectl exec -- python -c <constant> <argv...>`), so a path cannot become
anything but a path. The Pod has no route anywhere else, so the isolation does not rely on this.
"""

import json
from collections.abc import Callable, Iterable

from cmp_agents.redteam_agent import ObservedResult, ToolCall

from cmp_worker.sandbox_deploy import SandboxCluster
from cmp_worker.sandbox_isolation import Executor

PROBE_GET = "probe_get"
TOOL_NAMES = frozenset({PROBE_GET})
MAX_BODY = 2000

# One request, no redirects followed (a redirect to another address would simply be reported), a
# short timeout, and only status, a few headers and the start of the body are printed.
_PROBE_GET_PROGRAM = """
import http.client, json, sys
host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    c = http.client.HTTPConnection(host, port, timeout=5)
    c.request("GET", path, headers={"User-Agent": "cmp-redteam/1"})
    r = c.getresponse()
    keep = ("content-type", "location", "set-cookie", "www-authenticate", "server", "allow")
    head = {k: v for k, v in r.getheaders() if k.lower() in keep}
    body = r.read(__MAX_BODY__).decode("utf-8", "replace")
    print(json.dumps({"status": r.status, "headers": head, "body": body}))
except Exception as e:
    print(json.dumps({"error": type(e).__name__}))
""".replace("__MAX_BODY__", str(MAX_BODY))

_PROGRAMS = {PROBE_GET: _PROBE_GET_PROGRAM}


def make_dispatch(
    cluster: SandboxCluster, executor: Executor, *, known_secrets: Iterable[str] = ()
) -> Callable[[ToolCall], ObservedResult]:
    """Run one allowed tool in `executor`. An unknown tool name is refused before anything runs.

    The result is masked with the app's own secret values before it is returned, so it is safe to
    hand to a model and to store. A failure to run at all (the Pod is gone) raises
    `SandboxError`: the Agent loop treats that as a dead executor and stops.
    """
    secrets = list(known_secrets)

    def dispatch(call: ToolCall) -> ObservedResult:
        program = _PROGRAMS.get(call.tool)
        if program is None:
            return ObservedResult.from_raw("OUT_OF_SCOPE_TARGET")
        out = cluster.kubectl(
            "exec", "-n", executor.namespace, executor.name, "--",
            "python", "-c", program, executor.target_ip, str(executor.port), call.path,
            timeout=30,
        )  # fmt: skip
        text = out.decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise TypeError("tool output is not an object")
        except (ValueError, TypeError):
            return ObservedResult.from_raw("TOOL_OUTPUT_UNREADABLE", known_secrets=secrets)
        return ObservedResult.from_raw(json.dumps(parsed, sort_keys=True), known_secrets=secrets)

    return dispatch
