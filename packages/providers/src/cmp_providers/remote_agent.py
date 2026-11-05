"""Outbound on-premises Agent control client; approved rollout runs in `onprem_agent`."""

import argparse
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class AgentTransportError(ValueError):
    """Safe code-only diagnostics; never include tokens, response bodies or URLs."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AgentTransportError("REDIRECT_REJECTED")


class AgentControlClient:
    def __init__(self, base_url: str):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise AgentTransportError("SECURE_PLATFORM_URL_REQUIRED")
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(_RejectRedirects())

    @staticmethod
    def _id(value: str) -> str:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
            raise AgentTransportError("INVALID_AGENT_OR_JOB_ID")
        return value

    def _request(
        self,
        path: str,
        payload: dict | None,
        credential: str | None = None,
        *,
        raw=False,
        method: str | None = None,
    ):
        headers = {"Accept": "application/octet-stream" if raw else "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        method = method or ("GET" if raw else "POST")
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.read() if raw else json.load(response)
        except AgentTransportError:
            raise
        except HTTPError as exc:
            # Revoked/unknown credentials must be distinguishable so the Agent stops (ticket 15).
            code = "AGENT_UNAUTHORIZED" if exc.code in {401, 403} else "PLATFORM_REQUEST_FAILED"
            raise AgentTransportError(code) from None
        except (URLError, OSError, ValueError):
            raise AgentTransportError("PLATFORM_REQUEST_FAILED") from None

    def register(self, *, enrollment_token: str, agent_id: str, cluster_uid: str) -> str:
        self._id(agent_id)
        if not enrollment_token or not cluster_uid:
            raise AgentTransportError("INVALID_REGISTRATION_INPUT")
        response = self._request(
            "/agents/register",
            {
                "enrollment_token": enrollment_token,
                "agent_id": agent_id,
                "cluster_uid": cluster_uid,
            },
        )
        if response.get("agent_id") != agent_id or not isinstance(response.get("credential"), str):
            raise AgentTransportError("INVALID_PLATFORM_RESPONSE")
        return response["credential"]

    def poll_once(self, *, agent_id: str, credential: str) -> dict | None:
        self._id(agent_id)
        response = self._request(f"/agents/{agent_id}/jobs/claim", {}, credential)
        if not isinstance(response.get("job"), (dict, type(None))):
            raise AgentTransportError("INVALID_PLATFORM_RESPONSE")
        return response["job"]

    def report(self, *, agent_id: str, credential: str, job_id: str, status: str, code: str):
        self._id(agent_id)
        self._id(job_id)
        response = self._request(
            f"/agents/{agent_id}/jobs/{job_id}/result",
            {"status": status, "code": code},
            credential,
        )
        if response.get("job_id") != job_id or response.get("accepted") is not True:
            raise AgentTransportError("INVALID_PLATFORM_RESPONSE")

    def download_artifact(
        self, *, agent_id: str, credential: str, job_id: str, sha256: str
    ) -> bytes:
        """Exact bytes for one artifact of the caller's own job; the caller verifies the digest."""
        self._id(agent_id)
        self._id(job_id)
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise AgentTransportError("INVALID_ARTIFACT_DIGEST")
        return self._request(
            f"/agents/{agent_id}/jobs/{job_id}/artifacts/{sha256}", None, credential, raw=True
        )

    # --- tunnel exchange (ticket 16 API, used by the ticket 17 tunnel manager) ---------------
    def register_tunnel_key(self, *, agent_id: str, credential: str, public_key: str) -> dict:
        """Registers the WireGuard *public* key; the private key never leaves the Agent."""
        self._id(agent_id)
        response = self._request(
            f"/agents/{agent_id}/tunnel/key", {"public_key": public_key}, credential, method="PUT"
        )
        if not isinstance(response.get("tunnel_ip"), str):
            raise AgentTransportError("INVALID_PLATFORM_RESPONSE")
        return response

    def tunnel_config(self, *, agent_id: str, credential: str) -> dict:
        self._id(agent_id)
        response = self._request(
            f"/agents/{agent_id}/tunnel/config", None, credential, method="GET"
        )
        if not isinstance(response, dict) or not isinstance(response.get("relays"), list):
            raise AgentTransportError("INVALID_PLATFORM_RESPONSE")
        return response

    def report_tunnel_status(self, *, agent_id: str, credential: str, report: dict) -> None:
        self._id(agent_id)
        response = self._request(f"/agents/{agent_id}/tunnel/status", report, credential)
        if response.get("accepted") is not True:
            raise AgentTransportError("INVALID_PLATFORM_RESPONSE")


def _secret_file(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise AgentTransportError("PRIVATE_FILE_REQUIRED")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise AgentTransportError("EMPTY_CREDENTIAL_FILE")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Register and poll an on-premises Agent")
    parser.add_argument("action", choices=["enroll", "poll-once"])
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--cluster-uid")
    parser.add_argument("--enrollment-token-file", type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    args = parser.parse_args()
    client = AgentControlClient(args.platform_url)
    if args.action == "enroll":
        if not args.cluster_uid or not args.enrollment_token_file:
            parser.error("enroll requires --cluster-uid and --enrollment-token-file")
        if args.credential_file.exists():
            raise AgentTransportError("CREDENTIAL_FILE_EXISTS")
        token = _secret_file(args.enrollment_token_file)
        credential = client.register(
            enrollment_token=token, agent_id=args.agent_id, cluster_uid=args.cluster_uid
        )
        descriptor = os.open(args.credential_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(credential + "\n")
        print(json.dumps({"agent_id": args.agent_id, "registered": True}))
    else:
        credential = _secret_file(args.credential_file)
        job = client.poll_once(agent_id=args.agent_id, credential=credential)
        print(json.dumps({"job": job}, separators=(",", ":")))


if __name__ == "__main__":
    main()
