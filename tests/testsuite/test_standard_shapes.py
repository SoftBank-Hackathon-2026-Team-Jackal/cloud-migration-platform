"""Ticket 27: the standard tests follow the app's write API shape (Go demo app: server-made ID, 201,
idempotent re-send answers 200 with the stored row)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cmp_testsuite.runner import run_case
from cmp_testsuite.standard import AppApi, standard_cases

GO = {
    "health_path": "/healthz",
    "write_path": "/api/messages",
    "write_body": {
        "client_msg_id": "std-{{run_id}}",
        "author": "cmp-test",
        "body": "value-{{run_id}}",
    },
    "write_status": 201,
    "id_from_response": "id",
    "value_field": "body",
    "read_back": "REPEAT_WRITE",
}


class GoLike(BaseHTTPRequestHandler):
    rows: dict = {}  # noqa: RUF012 - shared on purpose: the handler class is the server's state
    next_id = [1]  # noqa: RUF012

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"status": "ok"}) if self.path == "/healthz" else self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = body.get("client_msg_id", "")
        if not 8 <= len(key) <= 64:
            return self._send(400, {"error": "client_msg_id must be 8-64 chars"})
        if key in self.rows:  # the app's idempotency: a re-send answers 200 with the stored row
            return self._send(200, self.rows[key])
        row = {"id": self.next_id[0], **body}
        self.next_id[0] += 1
        self.rows[key] = row
        return self._send(201, row)


@pytest.fixture
def server():
    GoLike.rows, GoLike.next_id = {}, [1]
    httpd = HTTPServer(("127.0.0.1", 0), GoLike)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def case(case_id):
    return next(c for c in standard_cases(AppApi(**GO)) if c.id == case_id)


def test_the_default_shape_is_unchanged_when_nothing_is_configured():
    plain = standard_cases(AppApi(health_path="/h", write_path="/w", read_path="/r/{{id}}"))
    write = next(c for c in plain if c.id == "std-db-roundtrip").steps[0]
    assert write.json_body == {"id": "std-{{run_id}}", "value": "value-{{run_id}}"}
    assert write.save == {} and write.expect.status is None


def test_the_go_shape_writes_with_201_and_reads_back_by_sending_the_same_write_again():
    write, again = case("std-db-roundtrip").steps
    assert write.expect.status == 201 and write.save == {"item_id": "id"}
    assert again.method == "POST" and again.path == "/api/messages"
    assert again.json_body == write.json_body
    assert again.expect.status == 200
    assert again.expect.json_equals == {"body": "value-{{run_id}}-rt"}
    assert again.expect.json_equals_var == {"id": "item_id"}


def test_each_case_has_its_own_key_so_an_idempotent_write_is_new_in_every_case():
    roundtrip, restart = case("std-db-roundtrip").steps, case("std-survives-db-restart").steps
    assert roundtrip[0].json_body["client_msg_id"] == "std-{{run_id}}-rt"
    assert restart[0].json_body["client_msg_id"] == "std-{{run_id}}-rs"
    assert restart[0].expect.status == 201 and restart[2].expect.status == 200
    assert restart[1].action == "RESTART_DATABASE"
    assert restart[2].json_body == restart[0].json_body  # the same write is sent again after


def test_an_app_that_keeps_its_data_passes_all_three_tests(server):
    variables = {"run_id": "abc123def456"}
    for cid in ("std-health", "std-db-roundtrip", "std-survives-db-restart"):
        c = next(x for x in standard_cases(AppApi(**GO)) if x.id == cid)
        result = run_case(
            c, server, actions={"RESTART_DATABASE": lambda: None}, variables=dict(variables)
        )
        assert result.status == "PASSED", (cid, result.reason)


def test_an_app_that_loses_its_data_at_the_restart_fails_the_restart_test_only(server):
    variables = {"run_id": "abc123def456"}
    lose = {"RESTART_DATABASE": lambda: GoLike.rows.clear()}
    assert run_case(case("std-db-roundtrip"), server, variables=dict(variables)).status == "PASSED"
    lost = run_case(
        case("std-survives-db-restart"), server, actions=lose, variables=dict(variables)
    )
    assert lost.status == "FAILED" and "STATUS_MISMATCH" in lost.reason  # 201 instead of 200


def test_an_app_that_answers_a_resend_with_a_different_row_fails_the_roundtrip(server):
    original = GoLike.do_POST

    def forgetful(self):  # accepts the write but stores nothing: the re-send is "new" again
        GoLike.rows.clear()
        original(self)

    GoLike.do_POST = forgetful
    try:
        result = run_case(case("std-db-roundtrip"), server, variables={"run_id": "abc123def456"})
    finally:
        GoLike.do_POST = original
    assert result.status == "FAILED"


# ------------------------------------------------------- recovery after the database restarts


class Recovering:
    """The app answers 503 for the first requests after the restart, as the demo app does."""

    def __init__(self, outage=3, lose=False):
        self.outage, self.lose, self.left = outage, lose, 0
        self.original = GoLike.do_POST

    def restart(self):
        self.left = self.outage
        if self.lose:
            GoLike.rows.clear()

    def install(self):
        outer, original = self, self.original

        def post(handler):
            if outer.left > 0:
                outer.left -= 1
                return handler._send(503, {"error": "temporarily unavailable, retry"})
            return original(handler)

        GoLike.do_POST = post

    def restore(self):
        GoLike.do_POST = self.original


@pytest.fixture
def recovering(server):
    holder = []

    def make(**kw):
        r = Recovering(**kw)
        r.install()
        holder.append(r)
        return r

    yield make
    for r in holder:
        r.restore()


def test_the_restart_case_waits_for_the_app_to_recover_instead_of_failing_on_the_first_503(
    recovering, server, monkeypatch
):
    monkeypatch.setattr("cmp_testsuite.runner._RETRY_PAUSE", 0.01)
    app = recovering(outage=3)
    result = run_case(
        case("std-survives-db-restart"), server,
        actions={"RESTART_DATABASE": app.restart}, variables={"run_id": "abc123def456"},
    )  # fmt: skip
    assert result.status == "PASSED", result.reason


def test_an_app_that_lost_its_data_fails_at_once_even_though_the_case_may_wait(
    recovering, server, monkeypatch
):
    monkeypatch.setattr("cmp_testsuite.runner._RETRY_PAUSE", 0.01)
    app = recovering(outage=0, lose=True)
    result = run_case(
        case("std-survives-db-restart"), server,
        actions={"RESTART_DATABASE": app.restart}, variables={"run_id": "abc123def456"},
    )  # fmt: skip
    assert result.status == "FAILED" and "expected=200 actual=201" in result.reason


def test_an_app_that_never_recovers_fails_after_the_wait(recovering, server, monkeypatch):
    monkeypatch.setattr("cmp_testsuite.runner._RETRY_PAUSE", 0.01)
    app = recovering(outage=10_000)
    steps = case("std-survives-db-restart").steps
    short = case("std-survives-db-restart").model_copy(
        update={"steps": [steps[0], steps[1].model_copy(update={"recover_seconds": 1}), steps[2]]}
    )
    result = run_case(
        short,
        server,
        actions={"RESTART_DATABASE": app.restart},
        variables={"run_id": "abc123def456"},
    )
    assert result.status == "FAILED" and "actual=503" in result.reason


def test_only_the_step_after_the_restart_waits(recovering, server, monkeypatch):
    monkeypatch.setattr("cmp_testsuite.runner._RETRY_PAUSE", 0.01)
    app = recovering(outage=2)
    app.left = 2  # already down before the case starts: the first write is not covered by the wait
    result = run_case(
        case("std-survives-db-restart"), server,
        actions={"RESTART_DATABASE": app.restart}, variables={"run_id": "abc123def456"},
    )  # fmt: skip
    assert result.status == "FAILED" and "actual=503" in result.reason
