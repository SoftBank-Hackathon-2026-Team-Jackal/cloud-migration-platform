"""Ticket 15 (#69): long-running Agent loop, credential storage and in-cluster target."""

import base64
import io
import json
from urllib.error import HTTPError

import pytest
import yaml
from cmp_providers.agent_runtime import (
    HALT_SECONDS,
    AgentDaemon,
    AgentRuntimeError,
    MemoryCredentialStore,
    SecretCredentialStore,
)
from cmp_providers.onprem_agent import ClusterTarget, ClusterUnavailable, RolloutAgentError
from cmp_providers.remote_agent import AgentControlClient, AgentTransportError


class Clock:
    """Records requested waits; returns True (stop) once the script is exhausted."""

    def __init__(self, stop_after: int):
        self.waits: list[float] = []
        self.stop_after = stop_after

    def __call__(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return len(self.waits) >= self.stop_after


class Registrar:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or ["cmp_agent_new"]
        self.calls = []

    def register(self, *, enrollment_token, agent_id, cluster_uid):
        self.calls.append((enrollment_token, agent_id, cluster_uid))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def daemon(store, poll, clock, registrar=None, discover=lambda: "uid-a"):
    return AgentDaemon(
        agent_id="agent-a",
        registrar=registrar or Registrar(),
        store=store,
        discover_cluster_uid=discover,
        poll=poll,
        wait=clock,
        log=lambda event: None,
    )


def scripted(*results):
    calls = []
    results = list(results)

    def poll(credential):
        calls.append(credential)
        result = results.pop(0) if results else None
        if isinstance(result, Exception):
            raise result
        return result

    poll.calls = calls
    return poll


def test_first_run_registers_with_discovered_uid_and_discards_the_token():
    store = MemoryCredentialStore(enrollment_token="cmp_enroll_once")
    registrar = Registrar("cmp_agent_saved")
    poll = scripted(None)
    assert daemon(store, poll, Clock(stop_after=1), registrar).run() == 0
    assert registrar.calls == [("cmp_enroll_once", "agent-a", "uid-a")]
    assert store.credential == "cmp_agent_saved" and store.enrollment_token is None
    assert poll.calls == ["cmp_agent_saved"]


def test_restart_reuses_the_stored_credential_without_registering():
    store = MemoryCredentialStore(credential="cmp_agent_saved", enrollment_token="stale")
    registrar = Registrar(AssertionError("must not register"))
    assert daemon(store, scripted(None), Clock(stop_after=1), registrar).run() == 0
    assert registrar.calls == []


def test_missing_credential_and_token_exits_with_a_code():
    with pytest.raises(AgentRuntimeError, match="ENROLLMENT_TOKEN_REQUIRED"):
        daemon(MemoryCredentialStore(), scripted(), Clock(stop_after=1)).run()


def test_rejected_enrollment_stops_without_retrying():
    registrar = Registrar(AgentTransportError("AGENT_UNAUTHORIZED"))
    store = MemoryCredentialStore(enrollment_token="cmp_enroll_used")
    clock = Clock(stop_after=2)
    assert daemon(store, scripted(), clock, registrar).run() == 2
    assert store.credential is None and len(registrar.calls) == 1
    assert clock.waits == [HALT_SECONDS, HALT_SECONDS]  # ticket 52: idle until SIGTERM


def test_platform_outage_backs_off_exponentially_with_a_cap_then_idles():
    failures = [AgentTransportError("PLATFORM_REQUEST_FAILED")] * 8
    clock = Clock(stop_after=9)
    store = MemoryCredentialStore(credential="cmp_agent_saved")
    assert daemon(store, scripted(*failures, None), clock).run() == 0
    assert clock.waits == [1, 2, 4, 8, 16, 32, 60, 60, 10]


def test_registration_outage_is_retried_with_backoff():
    registrar = Registrar(AgentTransportError("PLATFORM_REQUEST_FAILED"), "cmp_agent_saved")
    clock = Clock(stop_after=2)
    store = MemoryCredentialStore(enrollment_token="cmp_enroll_once")
    assert daemon(store, scripted(None), clock, registrar).run() == 0
    assert clock.waits == [1, 10] and store.credential == "cmp_agent_saved"


def test_cluster_unavailable_during_uid_discovery_is_retried():
    attempts = iter([ClusterUnavailable(), "uid-a"])

    def discover():
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    clock = Clock(stop_after=2)
    store = MemoryCredentialStore(enrollment_token="cmp_enroll_once")
    registrar = Registrar("cmp_agent_saved")
    assert daemon(store, scripted(None), clock, registrar, discover).run() == 0
    assert registrar.calls[0][2] == "uid-a" and clock.waits == [1, 10]


class FlakyStore(MemoryCredentialStore):
    """Kubernetes API unavailable for the first N calls of the named operations."""

    def __init__(self, failures: dict, **kwargs):
        super().__init__(**kwargs)
        self.failures = dict(failures)

    def _maybe_fail(self, operation):
        if self.failures.get(operation, 0) > 0:
            self.failures[operation] -= 1
            raise ClusterUnavailable()

    def load_credential(self):
        self._maybe_fail("load")
        return super().load_credential()

    def save_credential(self, credential):
        self._maybe_fail("save")
        super().save_credential(credential)


def test_unreachable_kubernetes_while_reading_the_credential_is_retried():
    store = FlakyStore({"load": 2}, credential="cmp_agent_saved")
    clock = Clock(stop_after=3)
    poll = scripted(None)
    assert daemon(store, poll, clock).run() == 0
    assert clock.waits == [1, 2, 10] and poll.calls == ["cmp_agent_saved"]


def test_issued_credential_is_kept_in_memory_until_it_is_saved():
    store = FlakyStore({"save": 2}, enrollment_token="cmp_enroll_once")
    registrar = Registrar("cmp_agent_issued", AssertionError("must not register twice"))
    clock = Clock(stop_after=3)
    assert daemon(store, scripted(None), clock, registrar).run() == 0
    assert len(registrar.calls) == 1 and store.credential == "cmp_agent_issued"
    assert store.enrollment_token is None and clock.waits == [1, 2, 10]


def test_processed_job_polls_again_immediately_and_success_resets_backoff():
    clock = Clock(stop_after=3)
    poll = scripted(
        AgentTransportError("PLATFORM_REQUEST_FAILED"),
        {"job_id": "job-a", "status": "SUCCEEDED", "code": "READY"},
        {"job_id": "job-b", "status": "FAILED", "code": "PVC_NOT_BOUND"},
        AgentTransportError("PLATFORM_REQUEST_FAILED"),
        None,
    )
    assert daemon(MemoryCredentialStore(credential="c"), poll, clock).run() == 0
    assert len(poll.calls) == 5
    assert clock.waits == [1, 1, 10]


def test_job_left_running_because_kubernetes_is_down_is_backed_off():
    clock = Clock(stop_after=2)
    poll = scripted({"job_id": "job-a", "status": "RUNNING", "code": "KUBERNETES_UNAVAILABLE"})
    assert daemon(MemoryCredentialStore(credential="c"), poll, clock).run() == 0
    assert clock.waits == [1, 10]


def test_revoked_credential_stops_polling_and_idles_instead_of_restarting():
    """Ticket 52 (D14, C-09): no exit (no restart loop, no repeated 403); SIGTERM ends it."""
    poll = scripted(AgentTransportError("AGENT_UNAUTHORIZED"), None)
    events = []
    clock = Clock(stop_after=3)
    agent = daemon(MemoryCredentialStore(credential="c"), poll, clock)
    agent.log = events.append
    assert agent.run() == 2
    assert len(poll.calls) == 1 and clock.waits == [HALT_SECONDS] * 3
    assert events == [{"event": "stopped", "code": "AGENT_UNAUTHORIZED"}]


def test_stop_requested_while_idle_exits_cleanly():
    clock = Clock(stop_after=1)
    poll = scripted(None, None)
    assert daemon(MemoryCredentialStore(credential="c"), poll, clock).run() == 0
    assert len(poll.calls) == 1


def test_stop_flag_is_checked_between_jobs():
    stop = {"now": False}
    poll_calls = []

    def poll(credential):
        poll_calls.append(credential)
        stop["now"] = True
        return {"job_id": "job-a", "status": "SUCCEEDED", "code": "READY"}

    agent = daemon(MemoryCredentialStore(credential="c"), poll, Clock(stop_after=99))
    agent.should_stop = lambda: stop["now"]
    assert agent.run() == 0 and len(poll_calls) == 1


# --- Kubernetes Secret credential store ---------------------------------------------------


class SecretCluster:
    def __init__(self, secrets=None):
        self.secrets = dict(secrets or {})
        self.applied = []
        self.deleted = []

    def get(self, kind, name, namespace=None):
        assert kind == "secret" and namespace == "cmp-system"
        data = self.secrets.get(name)
        if data is None:
            return None
        return {"data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}

    def apply(self, document):
        self.applied.append(document)
        name = document["metadata"]["name"]
        decoded = {k: base64.b64decode(v).decode() for k, v in document["data"].items()}
        self.secrets[name] = decoded

    def delete_secret(self, name, namespace):
        self.deleted.append((name, namespace))
        self.secrets.pop(name, None)


def test_secret_store_round_trip_keeps_values_out_of_arguments():
    cluster = SecretCluster({"cmp-agent-enrollment": {"token": "cmp_enroll_once"}})
    store = SecretCredentialStore(cluster, namespace="cmp-system")
    assert store.load_credential() is None
    assert store.load_enrollment_token() == "cmp_enroll_once"
    store.save_credential("cmp_agent_saved")
    store.discard_enrollment_token()
    [document] = cluster.applied
    assert document["metadata"]["name"] == "cmp-agent-credential"
    assert document["metadata"]["labels"]["cmp.dev/managed"] == "true"
    assert store.load_credential() == "cmp_agent_saved"
    assert store.load_enrollment_token() is None
    assert cluster.deleted == [("cmp-agent-enrollment", "cmp-system")]


def test_secret_store_rejects_empty_values():
    cluster = SecretCluster({"cmp-agent-enrollment": {"token": ""}})
    assert SecretCredentialStore(cluster, namespace="cmp-system").load_enrollment_token() is None


# --- in-cluster target ---------------------------------------------------------------------


def service_account(tmp_path):
    directory = tmp_path / "sa"
    directory.mkdir()
    (directory / "token").write_text("sa-token-value")
    (directory / "ca.crt").write_text("CA")
    return directory


def test_in_cluster_target_uses_a_generated_kubeconfig_with_the_token_file(tmp_path, monkeypatch):
    sa = service_account(tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    target = ClusterTarget.in_cluster(service_account_dir=sa, work_dir=tmp_path / "work")
    config = yaml.safe_load(target.kubeconfig.read_text())
    assert target.kubeconfig.stat().st_mode & 0o077 == 0
    [cluster] = config["clusters"]
    assert cluster["cluster"] == {
        "server": "https://10.43.0.1:443",
        "certificate-authority": str(sa / "ca.crt"),
    }
    [user] = config["users"]
    assert user["user"] == {"tokenFile": str(sa / "token")}  # rotation-safe, value not copied
    assert "sa-token-value" not in target.kubeconfig.read_text()
    assert config["current-context"] == target.context
    calls = []

    class Done:
        returncode = 0
        stdout = json.dumps({"metadata": {"uid": "uid-from-kube-system"}}).encode()
        stderr = b""

    monkeypatch.setattr(
        "cmp_providers.onprem_agent.subprocess.run",
        lambda argv, **kwargs: calls.append(argv) or Done(),
    )
    assert target.discover_cluster_uid() == "uid-from-kube-system"
    assert target.cluster_uid == "uid-from-kube-system"
    target.helm("status", "x")
    assert calls[0][:5] == ["kubectl", "--kubeconfig", str(target.kubeconfig), "--context",
                            target.context]  # fmt: skip
    assert calls[1][:5] == ["helm", "--kubeconfig", str(target.kubeconfig), "--kube-context",
                            target.context]  # fmt: skip


def test_in_cluster_target_requires_a_service_account(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    with pytest.raises(RolloutAgentError, match="SERVICE_ACCOUNT_REQUIRED"):
        ClusterTarget.in_cluster(service_account_dir=tmp_path / "missing", work_dir=tmp_path)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    with pytest.raises(RolloutAgentError, match="SERVICE_ACCOUNT_REQUIRED"):
        ClusterTarget.in_cluster(service_account_dir=service_account(tmp_path), work_dir=tmp_path)


def test_delete_secret_is_scoped_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    target = ClusterTarget.in_cluster(
        service_account_dir=service_account(tmp_path), work_dir=tmp_path / "w", cluster_uid="uid-a"
    )
    calls = []

    class Done:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(
        "cmp_providers.onprem_agent.subprocess.run",
        lambda argv, **kwargs: calls.append(argv) or Done(),
    )
    target.delete_secret("cmp-agent-enrollment", "cmp-system")
    assert calls[0][-6:] == [
        "delete",
        "secret",
        "cmp-agent-enrollment",
        "-n",
        "cmp-system",
        "--ignore-not-found",
    ]


# --- client: revocation is distinguishable ------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_client_reports_unauthorized_distinctly(status):
    client = AgentControlClient("https://platform.example.com")

    class Rejecting:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, status, "denied", {}, io.BytesIO(b"secret?"))

    client.opener = Rejecting()
    with pytest.raises(AgentTransportError, match="AGENT_UNAUTHORIZED"):
        client.poll_once(agent_id="agent-a", credential="cmp_agent_private")


def test_client_other_http_errors_stay_generic():
    client = AgentControlClient("https://platform.example.com")

    class Failing:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 503, "down", {}, io.BytesIO(b""))

    client.opener = Failing()
    with pytest.raises(AgentTransportError, match="PLATFORM_REQUEST_FAILED"):
        client.poll_once(agent_id="agent-a", credential="cmp_agent_private")


def test_finished_jobs_wake_the_tunnel_and_a_wake_failure_never_stops_the_agent():
    """Ticket 65 (#283): a processed job (not RUNNING, not idle) signals the tunnel container."""
    clock = Clock(stop_after=2)
    poll = scripted(
        {"job_id": "job-a", "status": "SUCCEEDED", "code": "READY"},
        {"job_id": "job-b", "status": "RUNNING", "code": "KUBERNETES_UNAVAILABLE"},
        {"job_id": "job-c", "status": "FAILED", "code": "PVC_NOT_BOUND"},
        None,
    )
    woken, events = [], []

    def wake(outcome):
        woken.append(outcome["job_id"])
        raise OSError("read-only")

    agent = daemon(MemoryCredentialStore(credential="c"), poll, clock)
    agent.wake, agent.log = wake, events.append
    assert agent.run() == 0
    assert woken == ["job-a", "job-c"]
    assert {"event": "tunnel_wake_failed"} in events
