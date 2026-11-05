import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from cmp_testsuite import (
    ActionStep,
    Expect,
    HttpStep,
    Suite,
    SuiteCase,
    SuiteError,
    dump_bundle,
    run_suite,
)
from cmp_worker.artifact_store import LocalArtifactStore


class App(BaseHTTPRequestHandler):
    items: ClassVar[dict[str, str]] = {}
    seen_auth: ClassVar[list[str | None]] = []

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        App.seen_auth.append(self.headers.get("Authorization"))
        if self.path == "/healthz":
            return self._send(200, {"status": "ok"})
        if self.path.startswith("/items/"):
            key = self.path.removeprefix("/items/")
            if key in App.items:
                return self._send(200, {"id": key, "value": App.items[key]})
        self._send(404, {"error": "no"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        App.items[body["id"]] = body["value"]
        self._send(201, {"id": body["id"]})


@pytest.fixture
def base_url():
    App.items, App.seen_auth = {}, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(tmp_path / "store")


HEALTH = SuiteCase(
    id="health", source="STANDARD", effect="READ_ONLY",
    steps=[HttpStep(path="/healthz", expect=Expect(status=200, json_equals={"status": "ok"}))],
)  # fmt: skip
ROUNDTRIP = SuiteCase(
    id="write-then-read", source="STANDARD", effect="WRITES_DATA",
    steps=[
        HttpStep(method="POST", path="/items", json_body={"id": "k-{{run_id}}", "value": "v-{{run_id}}"},
                 expect=Expect(status=201)),
        HttpStep(path="/items/k-{{run_id}}", expect=Expect(status=200, json_equals={"value": "v-{{run_id}}"})),
    ],
)  # fmt: skip
MAILER = SuiteCase(
    id="send-mail", source="AI", effect="WRITES_DATA", external_side_effects=True,
    steps=[HttpStep(path="/nowhere", expect=Expect(status=200))],
)  # fmt: skip


def pinned(store, *cases):
    return store.put(
        dump_bundle(Suite(name="demo", cases=list(cases))), ref_id="suite", kind="test-suite"
    )


def test_the_same_suite_always_gives_the_same_bytes():
    suite = Suite(name="demo", cases=[HEALTH, ROUNDTRIP])
    assert dump_bundle(suite) == dump_bundle(Suite(name="demo", cases=[ROUNDTRIP, HEALTH]))


def test_a_pinned_suite_runs_against_any_base_url(base_url, store):
    ref = pinned(store, HEALTH)
    result = run_suite(ref, base_url, store=store)
    assert result.suite_sha256 == ref.sha256 and result.passed
    assert [(r.id, r.status) for r in result.results] == [("health", "PASSED")]


def test_a_write_then_read_case_checks_the_stored_value_with_a_per_run_variable(base_url, store):
    result = run_suite(pinned(store, ROUNDTRIP), base_url, store=store)
    assert result.passed, result.results


def test_a_wrong_value_fails_with_a_code_and_without_response_content(base_url, store):
    wrong = SuiteCase(
        id="wrong", source="STANDARD", effect="READ_ONLY",
        steps=[HttpStep(path="/healthz", expect=Expect(json_equals={"status": "broken-value-xyz"}))],
    )  # fmt: skip
    result = run_suite(pinned(store, wrong), base_url, store=store)
    assert not result.passed
    assert result.results[0].reason == "JSON_MISMATCH status"
    assert "ok" not in (result.results[0].reason or "").split()


def test_a_changed_bundle_is_refused(base_url, store):
    ref = pinned(store, HEALTH)
    (store.root / ref.sha256).write_bytes(
        dump_bundle(Suite(name="demo", cases=[HEALTH, ROUNDTRIP]))
    )
    with pytest.raises(SuiteError) as exc:
        run_suite(ref, base_url, store=store)
    assert exc.value.code == "SUITE_HASH_MISMATCH"


def test_cases_with_external_side_effects_are_skipped_unless_asked(base_url, store):
    ref = pinned(store, HEALTH, MAILER)
    default = run_suite(ref, base_url, store=store)
    assert {r.id: r.status for r in default.results} == {"health": "PASSED", "send-mail": "SKIPPED"}
    explicit = run_suite(ref, base_url, store=store, include_external=True)
    assert {r.id: r.status for r in explicit.results}["send-mail"] == "FAILED"


def test_read_only_include_leaves_out_writing_cases(base_url, store):
    result = run_suite(pinned(store, HEALTH, ROUNDTRIP), base_url, include="READ_ONLY", store=store)
    assert {r.id: r.status for r in result.results} == {
        "health": "PASSED",
        "write-then-read": "SKIPPED",
    }
    assert App.items == {}


def test_auth_is_resolved_from_a_reference_and_sent_as_a_header(base_url, store):
    run_suite(pinned(store, HEALTH), base_url, "ref-1", store=store,
              resolve_auth=lambda ref: {"Authorization": "Bearer from-" + ref})  # fmt: skip
    assert App.seen_auth == ["Bearer from-ref-1"]
    with pytest.raises(SuiteError):
        run_suite(pinned(store, HEALTH), base_url, "ref-1", store=store)


def test_an_action_step_needs_an_implementation(base_url, store):
    restart = SuiteCase(
        id="persist", source="STANDARD", effect="READ_ONLY",
        steps=[ActionStep(action="RESTART_DATABASE"), HttpStep(path="/healthz", expect=Expect(status=200))],
    )  # fmt: skip
    ref = pinned(store, restart)
    assert (
        run_suite(ref, base_url, store=store).results[0].reason
        == "ACTION_UNAVAILABLE RESTART_DATABASE"
    )
    calls = []
    ok = run_suite(
        ref, base_url, store=store, actions={"RESTART_DATABASE": lambda: calls.append(1)}
    )
    assert ok.passed and calls == [1]
