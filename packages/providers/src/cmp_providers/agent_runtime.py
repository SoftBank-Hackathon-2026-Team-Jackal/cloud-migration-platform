"""Long-running on-premises Agent (ticket 15, #69; decisions H1/H5/H6).

Registers once with a one-time enrollment token, keeps the issued credential, then polls the
platform outbound forever: each claimed job is handled by `onprem_agent.RolloutAgent` (ticket 12
semantics: verify-only on reclaim, never re-apply an interrupted Release). Log lines and exit
codes carry codes only — never tokens, credentials, URLs or kubectl/helm output.
"""

import argparse
import base64
import json
import os
import secrets
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from cmp_providers.agent_image_pull import PullSecretRefresher
from cmp_providers.agent_preflight import PreflightJobRunner, probe_executor_factory
from cmp_providers.onprem_agent import (
    MANAGED,
    ClusterTarget,
    ClusterUnavailable,
    RolloutAgent,
    RolloutAgentError,
)
from cmp_providers.remote_agent import AgentControlClient, AgentTransportError, _secret_file

UNAUTHORIZED_EXIT = 2
# Ticket 52 (D14, C-09): a revoked/rejected Agent idles in these steps until SIGTERM instead of
# exiting into a restart loop that keeps sending 403 requests.
HALT_SECONDS = 3600


def halt(wait: Callable[[float], bool]) -> int:
    """Block without platform requests until a stop is requested; then exit with code 2."""
    while not wait(HALT_SECONDS):
        pass
    return UNAUTHORIZED_EXIT


def read_wake(path) -> str | None:
    """Token the `agent` container last wrote to the shared signal file (None if absent)."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(64).strip() or None
    except OSError:
        return None


def write_wake(path) -> None:
    """Agent side (ticket 65): a new token tells the tunnel to re-check soon. Atomic replace."""
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(f"{time.time_ns()}-{secrets.token_hex(4)}")
    os.replace(temporary, path)


class AgentRuntimeError(Exception):
    """Configuration problem the Agent cannot recover from by retrying (code only)."""


class CredentialStore(Protocol):
    def load_credential(self) -> str | None: ...
    def load_enrollment_token(self) -> str | None: ...
    def save_credential(self, credential: str) -> None: ...
    def discard_enrollment_token(self) -> None: ...


class MemoryCredentialStore:
    def __init__(self, *, credential: str | None = None, enrollment_token: str | None = None):
        self.credential, self.enrollment_token = credential, enrollment_token

    def load_credential(self):
        return self.credential

    def load_enrollment_token(self):
        return self.enrollment_token

    def save_credential(self, credential):
        self.credential = credential

    def discard_enrollment_token(self):
        self.enrollment_token = None


class FileCredentialStore:
    """Host CLI: owner-only (0600) files, as in ticket 04A."""

    def __init__(self, credential_file: Path, enrollment_token_file: Path | None = None):
        self.credential_file, self.token_file = Path(credential_file), enrollment_token_file

    def load_credential(self):
        return _secret_file(self.credential_file) if self.credential_file.exists() else None

    def load_enrollment_token(self):
        if self.token_file is None or not Path(self.token_file).exists():
            return None
        return _secret_file(Path(self.token_file))

    def save_credential(self, credential):
        descriptor = os.open(self.credential_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(credential + "\n")

    def discard_enrollment_token(self):
        if self.token_file is not None:
            Path(self.token_file).unlink(missing_ok=True)


class SecretCredentialStore:
    """In-cluster (H5): the Chart creates only the enrollment-token Secret. The credential lives
    in a Secret Helm does not manage, so `helm upgrade` never overwrites it."""

    def __init__(
        self,
        target,
        *,
        namespace: str,
        credential_secret: str = "cmp-agent-credential",
        enrollment_secret: str = "cmp-agent-enrollment",
    ):
        self.target, self.namespace = target, namespace
        self.credential_secret, self.enrollment_secret = credential_secret, enrollment_secret

    def _read(self, name: str, key: str) -> str | None:
        secret = self.target.get("secret", name, self.namespace)
        encoded = ((secret or {}).get("data") or {}).get(key)
        if not encoded:
            return None
        value = base64.b64decode(encoded).decode("utf-8").strip()
        return value or None

    def load_credential(self):
        return self._read(self.credential_secret, "credential")

    def load_enrollment_token(self):
        return self._read(self.enrollment_secret, "token")

    def save_credential(self, credential):
        # Sent to kubectl on stdin (apply -f -), never as an argument.
        self.target.apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": self.credential_secret,
                    "namespace": self.namespace,
                    "labels": {MANAGED: "true"},
                },
                "data": {"credential": base64.b64encode(credential.encode()).decode()},
            }
        )

    def discard_enrollment_token(self):
        self.target.delete_secret(self.enrollment_secret, self.namespace)


class AgentDaemon:
    """`poll(credential)` handles at most one job and returns its outcome dict or None (idle).
    `wait(seconds)` sleeps and returns True when a stop was requested."""

    def __init__(
        self,
        *,
        agent_id: str,
        registrar,
        store: CredentialStore,
        discover_cluster_uid: Callable[[], str],
        poll: Callable[[str], dict | None],
        wait: Callable[[float], bool],
        log: Callable[[dict], None],
        idle_seconds: float = 10,
        max_backoff: float = 60,
    ):
        self.agent_id, self.registrar, self.store = agent_id, registrar, store
        self.discover_cluster_uid, self.poll = discover_cluster_uid, poll
        self.wait, self.log = wait, log
        self.idle_seconds, self.max_backoff = idle_seconds, max_backoff
        self.should_stop: Callable[[], bool] = lambda: False
        # Ticket 65 (#283): called after each processed job so the tunnel re-checks soon.
        self.wake: Callable[[dict], None] | None = None
        self._failures = 0

    def _backoff(self, code: str) -> bool:
        delay = min(2**self._failures, self.max_backoff)
        self._failures += 1
        self.log({"event": "retry", "code": code, "seconds": delay})
        return self.wait(delay)

    def _attempt(self, operation):
        """Retry `operation` through Kubernetes/platform outages with backoff.
        Returns (True, value), or (False, None) when a stop was requested while waiting."""
        while not self.should_stop():
            try:
                value = operation()
            except ClusterUnavailable:
                if self._backoff("KUBERNETES_UNAVAILABLE"):
                    return False, None
                continue
            except AgentTransportError as exc:
                if str(exc) == "AGENT_UNAUTHORIZED":
                    raise
                if self._backoff(str(exc)):
                    return False, None
                continue
            self._failures = 0
            return True, value
        return False, None

    def _credential(self) -> str | None:
        """Returns the credential, or None if a stop was requested while retrying."""
        ok, credential = self._attempt(self.store.load_credential)
        if not ok or credential:
            return credential if ok else None
        ok, token = self._attempt(self.store.load_enrollment_token)
        if not ok:
            return None
        if not token:
            raise AgentRuntimeError("ENROLLMENT_TOKEN_REQUIRED")

        def register() -> str:
            return self.registrar.register(
                enrollment_token=token,
                agent_id=self.agent_id,
                cluster_uid=self.discover_cluster_uid(),
            )

        ok, credential = self._attempt(register)
        if not ok:
            return None
        # The token is now spent: keep the issued credential in memory until it is stored.
        ok, _ = self._attempt(lambda: self.store.save_credential(credential))
        if not ok:
            self.log({"event": "stopped", "code": "CREDENTIAL_NOT_SAVED"})
            return None
        self._attempt(self.store.discard_enrollment_token)
        self.log({"event": "registered", "agent_id": self.agent_id})
        return credential

    def run(self) -> int:
        try:
            credential = self._credential()
        except AgentTransportError:
            self.log({"event": "stopped", "code": "ENROLLMENT_REJECTED"})
            return halt(self.wait)
        if credential is None:
            return 0
        while not self.should_stop():
            try:
                outcome = self.poll(credential)
            except AgentTransportError as exc:
                if str(exc) == "AGENT_UNAUTHORIZED":
                    self.log({"event": "stopped", "code": "AGENT_UNAUTHORIZED"})
                    return halt(self.wait)
                if self._backoff(str(exc)):
                    return 0
                continue
            if outcome is None:
                self._failures = 0
                if self.wait(self.idle_seconds):
                    return 0
                continue
            self.log({"event": "job", **outcome})
            if outcome.get("status") == "RUNNING":
                # Kubernetes was unreachable before any change; the job stays RUNNING.
                if self._backoff(outcome.get("code", "KUBERNETES_UNAVAILABLE")):
                    return 0
                continue
            if self.wake is not None:
                try:
                    self.wake(outcome)
                except OSError:
                    self.log({"event": "tunnel_wake_failed"})  # the tunnel's cycle still runs
            self._failures = 0  # processed: poll again immediately
        return 0


def _log(event: dict) -> None:
    print(json.dumps(event, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Long-running on-premises Agent")
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--in-cluster", action="store_true")
    parser.add_argument("--namespace", default="cmp-system", help="in-cluster Secret namespace")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--cluster-uid")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--enrollment-token-file", type=Path)
    parser.add_argument("--helm-timeout", default="5m")
    parser.add_argument("--idle-seconds", type=float, default=10)
    # MNP-01 N3: digest-pinned probe image for NETWORK_PREFLIGHT jobs and its allowlist.
    parser.add_argument("--preflight-image", default=os.environ.get("CMP_PREFLIGHT_IMAGE", ""))
    parser.add_argument(
        "--preflight-allowed-images",
        default=os.environ.get("CMP_PREFLIGHT_ALLOWED_IMAGES", ""),
        help="comma-separated digest-pinned images",
    )
    parser.add_argument("--tunnel-wake-file", help="signal file read by the tunnel (ticket 65)")
    args = parser.parse_args(argv)
    try:
        client = AgentControlClient(args.platform_url)
        if args.in_cluster:
            target = ClusterTarget.in_cluster(
                cluster_uid=args.cluster_uid, helm_timeout=args.helm_timeout
            )
            store = SecretCredentialStore(target, namespace=args.namespace)
        else:
            if not (args.kubeconfig and args.context and args.credential_file):
                parser.error("host mode requires --kubeconfig, --context and --credential-file")
            target = ClusterTarget(
                args.kubeconfig, args.context, args.cluster_uid or "", args.helm_timeout
            )
            store = FileCredentialStore(args.credential_file, args.enrollment_token_file)
    except (AgentTransportError, RolloutAgentError) as exc:
        _log({"event": "stopped", "code": str(exc)})
        return UNAUTHORIZED_EXIT

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        # Only flags the loop: a running Helm/kubectl step finishes before the Agent exits.
        signal.signal(signum, lambda *_: stop.set())

    def discover() -> str:
        return target.cluster_uid if target.cluster_uid else target.discover_cluster_uid()

    allowed = frozenset(filter(None, args.preflight_allowed_images.split(",")))
    preflight = PreflightJobRunner(
        target, probe_executor_factory(target, args.preflight_image, allowed)
    )

    refresher = PullSecretRefresher(target, fetch=None)

    def poll(credential: str) -> dict | None:
        if not target.cluster_uid:
            target.discover_cluster_uid()
        outcome = RolloutAgent(args.agent_id, credential, client, target, preflight).poll_once()
        refresh_pull_secrets(refresher, client, args.agent_id, credential, _log)
        return outcome

    daemon = AgentDaemon(
        agent_id=args.agent_id,
        registrar=client,
        store=store,
        discover_cluster_uid=discover,
        poll=lambda credential: _poll_safely(poll, credential),
        wait=stop.wait,
        log=_log,
        idle_seconds=args.idle_seconds,
    )
    daemon.should_stop = stop.is_set
    if args.tunnel_wake_file:
        daemon.wake = lambda outcome: write_wake(args.tunnel_wake_file)
    try:
        return daemon.run()
    except AgentRuntimeError as exc:
        _log({"event": "stopped", "code": str(exc)})
        return UNAUTHORIZED_EXIT


def refresh_pull_secrets(refresher: PullSecretRefresher, client, agent_id, credential, log):
    """Ticket 29 (I6): after every poll, refresh managed ECR pull Secrets past half their TTL.
    Never stops the poll loop except for AGENT_UNAUTHORIZED; logs codes and Namespaces only."""

    def fetch(job_id: str) -> dict:
        return client.image_pull(agent_id=agent_id, credential=credential, job_id=job_id)

    refresher.fetch = fetch
    try:
        events = refresher.refresh()
    except ClusterUnavailable:
        events = [{"event": "image_pull_refresh", "code": "KUBERNETES_UNAVAILABLE"}]
    for event in events:
        log(event)


def _poll_safely(poll, credential):
    try:
        return poll(credential)
    except ClusterUnavailable:
        return {"status": "RUNNING", "code": "KUBERNETES_UNAVAILABLE"}


if __name__ == "__main__":
    sys.exit(main())
