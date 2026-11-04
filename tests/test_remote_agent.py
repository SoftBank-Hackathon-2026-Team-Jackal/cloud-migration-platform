"""Outbound Agent client uses HTTPS and keeps credentials in headers/body."""

import io
import json

import pytest
from cmp_providers.remote_agent import AgentControlClient, AgentTransportError


class Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if request.full_url.endswith("/agents/register"):
            response = {"agent_id": "agent-a", "credential": "cmp_agent_private"}
        elif request.full_url.endswith("/jobs/claim"):
            response = {"job": {"job_id": "job-a", "status": "RUNNING"}}
        else:
            response = {"job_id": "job-a", "accepted": True}
        return io.BytesIO(json.dumps(response).encode())


def test_outbound_register_poll_and_report():
    client = AgentControlClient("https://platform.example.com")
    opener = Opener()
    client.opener = opener
    credential = client.register(
        enrollment_token="cmp_enroll_private", agent_id="agent-a", cluster_uid="cluster-a"
    )
    job = client.poll_once(agent_id="agent-a", credential=credential)
    client.report(
        agent_id="agent-a",
        credential=credential,
        job_id=job["job_id"],
        status="SUCCEEDED",
        code="OK",
    )
    assert len(opener.requests) == 3
    assert "cmp_enroll_private" not in opener.requests[0].full_url
    assert "cmp_agent_private" not in opener.requests[1].full_url
    assert opener.requests[1].get_header("Authorization") == "Bearer cmp_agent_private"


def test_nonloopback_http_and_path_injection_are_rejected():
    with pytest.raises(AgentTransportError, match="SECURE_PLATFORM_URL_REQUIRED"):
        AgentControlClient("http://platform.example.com")
    client = AgentControlClient("http://127.0.0.1:8000")
    with pytest.raises(AgentTransportError, match="INVALID_AGENT_OR_JOB_ID"):
        client.poll_once(agent_id="../other", credential="private")


def test_artifact_download_is_authenticated_get_of_own_job_digest():
    client = AgentControlClient("https://platform.example.com")
    requests = []

    class BytesOpener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(b"chart-bytes")

    client.opener = BytesOpener()
    digest = "a" * 64
    content = client.download_artifact(
        agent_id="agent-a", credential="cmp_agent_private", job_id="job-a", sha256=digest
    )
    assert content == b"chart-bytes"
    [request] = requests
    assert request.get_method() == "GET"
    assert request.full_url.endswith(f"/agents/agent-a/jobs/job-a/artifacts/{digest}")
    assert "cmp_agent_private" not in request.full_url
    with pytest.raises(AgentTransportError, match="INVALID_ARTIFACT_DIGEST"):
        client.download_artifact(
            agent_id="agent-a", credential="x", job_id="job-a", sha256="../etc/passwd"
        )
