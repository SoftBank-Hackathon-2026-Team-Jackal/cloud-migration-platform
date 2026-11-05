"""On-prem Agent ECR pull Secret: create before Helm, refresh while running (ticket 29, I5–I7).

The Agent holds no AWS credentials. It fetches a short-lived token for its own job from the
platform (`GET /agents/{id}/jobs/{job}/image-pull`), writes the docker-registry Secret through
stdin and links it to the Namespace `default` ServiceAccount (both Charts use it). State needed
for refresh lives in non-secret annotations, so a restarted Agent keeps refreshing. Only codes
are logged; the token never leaves memory except to the cluster API.
"""

import base64
import json
import time
from collections.abc import Callable

from cmp_providers.ecr_pull import dockerconfigjson

MANAGED = "cmp.dev/managed"
PURPOSE = "cmp.dev/purpose"
PULL_PURPOSE = "image-pull"
JOB_ID = "cmp.dev/job-id"
ISSUED_AT = "cmp.dev/pull-issued-at"
EXPIRES_AT = "cmp.dev/pull-expires-at"
REGISTRY = "cmp.dev/pull-registry"
PULL_FAILURES = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
RETRY_SECONDS = 300


class ImagePullError(Exception):
    def __init__(self, code: str, status: str = "FAILED"):
        super().__init__(code)
        self.code, self.status = code, status


def validate(credential: object, registry: str) -> dict:
    """The platform response must be for the job's registry and not already expired."""
    if (
        not isinstance(credential, dict)
        or credential.get("registry") != registry
        or not all(
            isinstance(credential.get(k), str) and credential[k] for k in ("username", "password")
        )
        or not isinstance(credential.get("expires_at"), int)
    ):
        raise ImagePullError("IMAGE_PULL_CREDENTIALS_INVALID")
    return credential


def conflict(target, namespace: str, name: str) -> bool:
    """True when a Secret with this name exists and the platform does not manage it."""
    existing = target.get("secret", name, namespace)
    labels = ((existing or {}).get("metadata") or {}).get("labels") or {}
    return existing is not None and labels.get(MANAGED) != "true"


def apply_pull_secret(
    target, namespace: str, name: str, job_id: str, credential: dict, now: float
) -> None:
    config = dockerconfigjson(
        credential["registry"], credential["username"], credential["password"]
    )
    target.apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "kubernetes.io/dockerconfigjson",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {MANAGED: "true", PURPOSE: PULL_PURPOSE},
                "annotations": {
                    JOB_ID: job_id,
                    ISSUED_AT: str(int(now)),
                    EXPIRES_AT: str(credential["expires_at"]),
                    REGISTRY: credential["registry"],
                },
            },
            "data": {".dockerconfigjson": base64.b64encode(config.encode()).decode()},
        }
    )
    del config


def link_service_account(target, namespace: str, name: str) -> None:
    target.apply(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "default", "namespace": namespace},
            "imagePullSecrets": [{"name": name}],
        }
    )


def image_pull_failed(target, namespace: str, release: str) -> bool:
    """Pods of this Release waiting on an image pull (09/11 `PULL_FAILURES`)."""
    try:
        result = target.kubectl(
            "get", "pods", "-n", namespace,
            "-l", f"app.kubernetes.io/instance={release}", "-o", "json",
        )  # fmt: skip
        if result.returncode or not result.stdout.strip():
            return False
        pods = json.loads(result.stdout).get("items", [])
    except Exception:  # noqa: BLE001 - diagnostics only; fall back to WORKLOAD_NOT_READY
        return False
    for pod in pods:
        status = pod.get("status") or {}
        for container in [
            *(status.get("initContainerStatuses") or []),
            *(status.get("containerStatuses") or []),
        ]:
            if ((container.get("state") or {}).get("waiting") or {}).get("reason") in PULL_FAILURES:
                return True
    return False


class PullSecretRefresher:
    """Re-fetches every managed pull Secret once half its TTL has passed (I6).

    `fetch(job_id) -> credential dict` calls the platform for that job; `list_secrets()`
    returns the managed pull Secrets (`cmp.dev/purpose=image-pull`) across Namespaces.
    """

    def __init__(
        self, target, fetch: Callable[[str], dict], clock: Callable[[], float] = time.time
    ):
        self.target, self.fetch, self.clock = target, fetch, clock
        self._retry_after: dict[tuple[str, str], float] = {}

    def due(self, secret: dict, now: float) -> bool:
        annotations = (secret.get("metadata") or {}).get("annotations") or {}
        try:
            issued, expires = int(annotations[ISSUED_AT]), int(annotations[EXPIRES_AT])
        except (KeyError, ValueError):
            return True
        return now >= issued + (expires - issued) / 2

    def refresh(self) -> list[dict]:
        """One pass. Returns code-only events; raises AgentTransportError AGENT_UNAUTHORIZED."""
        from cmp_providers.remote_agent import AgentTransportError

        now, events = self.clock(), []
        for secret in self.target.list_pull_secrets():
            metadata = secret.get("metadata") or {}
            labels = metadata.get("labels") or {}
            if labels.get(MANAGED) != "true" or labels.get(PURPOSE) != PULL_PURPOSE:
                continue
            namespace, name = metadata.get("namespace"), metadata.get("name")
            annotations = metadata.get("annotations") or {}
            job_id, registry = annotations.get(JOB_ID), annotations.get(REGISTRY)
            key = (namespace, name)
            if not (namespace and name and job_id and registry) or not self.due(secret, now):
                continue
            if self._retry_after.get(key, 0) > now:
                continue
            try:
                credential = validate(self.fetch(job_id), registry)
                apply_pull_secret(self.target, namespace, name, job_id, credential, now)
                del credential
            except AgentTransportError as exc:
                if str(exc) == "AGENT_UNAUTHORIZED":
                    raise
                self._retry_after[key] = now + RETRY_SECONDS
                events.append(
                    {"event": "image_pull_refresh", "code": str(exc), "namespace": namespace}
                )
                continue
            except ImagePullError as exc:
                self._retry_after[key] = now + RETRY_SECONDS
                events.append(
                    {"event": "image_pull_refresh", "code": exc.code, "namespace": namespace}
                )
                continue
            except Exception:  # noqa: BLE001 - cluster errors: codes only, try again later
                self._retry_after[key] = now + RETRY_SECONDS
                events.append(
                    {
                        "event": "image_pull_refresh",
                        "code": "KUBERNETES_UNAVAILABLE",
                        "namespace": namespace,
                    }
                )
                continue
            self._retry_after.pop(key, None)
            events.append(
                {"event": "image_pull_refresh", "code": "REFRESHED", "namespace": namespace}
            )
        return events
