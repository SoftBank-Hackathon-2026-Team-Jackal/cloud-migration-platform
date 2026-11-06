"""Ticket 27-B: answers recorded from the app, never guessed by a model.

A `Probe` is only a request the Tester wants answered (plus which answer fields change from call
to call). The platform sends it to the app twice, and what both calls agree on becomes the test's
expected values. The model never writes an expected value for these tests.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from cmp_testsuite import run_case
from cmp_testsuite.model import Evidence
from cmp_testsuite.record import Probe, record_probe

EVIDENCE = [Evidence(file="main.go", line=4)]


def probe(path="/items", volatile=(), pid="items"):
    return Probe(id=pid, path=path, volatile=list(volatile), evidence=EVIDENCE)


def answers(*replies):
    """A fetch that gives the given (status, body) answers one after another."""
    queue = list(replies)

    def fetch(path):
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()

    return fetch


def test_two_equal_answers_become_the_expected_values():
    body = {"count": 2, "items": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
    out = record_probe(probe(), answers((200, body)))
    assert out.reason is None
    case = out.case
    assert (case.id, case.source, case.effect, case.basis) == (
        "ai-0rec-items",
        "AI",
        "READ_ONLY",
        "RECORDED",
    )
    assert case.evidence == EVIDENCE
    (step,) = case.steps
    assert (step.method, step.path, step.expect.status) == ("GET", "/items", 200)
    assert step.expect.json_equals == {
        "count": 2,
        "items.0.id": 1,
        "items.0.name": "a",
        "items.1.id": 2,
        "items.1.name": "b",
    }


def test_fields_the_tester_marked_as_changing_are_left_out_and_cannot_make_it_unstable():
    first = {"name": "a", "at": "10:00", "meta": {"etag": "1", "n": 1}}
    second = {"name": "a", "at": "10:01", "meta": {"etag": "2", "n": 2}}
    out = record_probe(probe(volatile=["at", "meta"]), answers((200, first), (200, second)))
    assert out.reason is None
    assert out.case.steps[0].expect.json_equals == {"name": "a"}


def test_a_field_that_changes_without_being_marked_refuses_the_probe():
    first, second = {"name": "a", "at": "10:00"}, {"name": "a", "at": "10:01"}
    out = record_probe(probe(), answers((200, first), (200, second)))
    assert (out.case, out.reason) == (None, "RECORD_UNSTABLE at")


@pytest.mark.parametrize("replies", [[(500, {"a": 1})], [(200, {"a": 1}), (404, {"a": 1})]])
def test_only_a_success_both_times_is_recorded(replies):
    out = record_probe(probe(), answers(*replies))
    assert out.case is None and out.reason.startswith("RECORD_STATUS")


def test_a_body_that_is_not_json_is_refused():
    assert record_probe(probe(), answers((200, b"hello"))).reason == "RECORD_NOT_JSON"


def test_a_body_with_nothing_to_compare_is_refused():
    assert record_probe(probe(), answers((200, {}))).reason == "RECORD_EMPTY"


def test_an_answer_with_a_sensitive_looking_value_is_not_stored():
    out = record_probe(
        probe(),
        answers((200, {"token": "sk-live-123", "n": 1})),
        sensitive=lambda text: text.startswith("sk-"),
    )
    assert (out.case, out.reason) == (None, "RECORD_SENSITIVE")


def test_a_huge_answer_is_refused_rather_than_stored_whole():
    out = record_probe(probe(), answers((200, {"rows": list(range(500))})))
    assert out.reason == "RECORD_TOO_LARGE"


def test_a_probe_path_must_be_a_plain_path():
    with pytest.raises(ValueError):
        Probe(id="x", path="/items/{{id}}", evidence=EVIDENCE)
    with pytest.raises(ValueError):
        Probe(id="x", path="items", evidence=EVIDENCE)
    with pytest.raises(ValueError):
        Probe(id="x", path="/items", evidence=[])


# --------------------------------------------------------- the recorded test, run for real


class App(BaseHTTPRequestHandler):
    reply: ClassVar[dict] = {"count": 1}

    def do_GET(self):
        body = json.dumps(App.reply).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    App.reply = {"count": 1, "name": "a"}
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_the_recorded_test_passes_while_the_app_answers_the_same_and_fails_when_it_changes(server):
    import urllib.request

    def fetch(path):
        with urllib.request.urlopen(server + path) as response:
            return response.status, response.read()

    case = record_probe(probe(), fetch).case
    assert run_case(case, server).status == "PASSED"
    App.reply = {"count": 2, "name": "a"}
    result = run_case(case, server)
    assert (result.status, result.reason) == ("FAILED", "JSON_MISMATCH count")
