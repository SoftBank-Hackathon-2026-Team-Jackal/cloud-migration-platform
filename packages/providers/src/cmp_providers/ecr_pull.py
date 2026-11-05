"""Platform-minted ECR pull credentials (ticket 29, I1/I2).

Only the platform holds AWS permissions. It calls `ecr:GetAuthorizationToken` with its own
principal for the configured platform registry and hands the short-lived token to the executing
side (the on-prem Agent), which creates and refreshes the pull Secret. The token lives only in a
`PullCredential` in memory: never in `repr`, logs, results, the job body or Temporal history.
"""

import base64
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

PULL_SECRET = "cmp-ecr-pull"
DEFAULT_REPOSITORIES = "cmp-platform-builds"
REGISTRY_HOST = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$"
)
ECR_IMAGE = re.compile(
    r"^(?P<registry>[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com)/"
    r"(?P<repository>[a-z0-9][a-z0-9._/-]*?)(?:[:@].*)?$"
)
REPOSITORY = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,255}$")


class EcrPullError(ValueError):
    """Code-only error; never carries tokens or AWS error messages."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def ecr_registry(uri: str) -> tuple[str, str] | None:
    """(registry host, repository) for an ECR image URI, else None."""
    match = ECR_IMAGE.match(uri)
    return (match["registry"], match["repository"]) if match else None


@dataclass(frozen=True)
class PlatformRegistry:
    """The platform's own ECR registry and the repositories its images may come from."""

    registry: str
    repositories: frozenset[str]

    @property
    def region(self) -> str:
        return REGISTRY_HOST.match(self.registry)["region"]

    @classmethod
    def parse(
        cls, registry: str | None, repositories: str | None = DEFAULT_REPOSITORIES
    ) -> "PlatformRegistry | None":
        if not registry:
            return None
        names = frozenset(filter(None, (n.strip() for n in (repositories or "").split(","))))
        if not REGISTRY_HOST.match(registry) or not names or not all(map(REPOSITORY.match, names)):
            raise EcrPullError("PLATFORM_REGISTRY_INVALID")
        return cls(registry, names)

    def required(self, uris: Iterable[str]) -> str | None:
        """The registry needing a pull Secret, None when no image is in ECR. ECR images outside
        the platform registry/repositories cannot be pulled with the platform role: refused."""
        return PlatformRegistry.require(self, uris)

    @staticmethod
    def require(registry: "PlatformRegistry | None", uris: Iterable[str]) -> str | None:
        found = [parsed for parsed in map(ecr_registry, uris) if parsed is not None]
        if not found:
            return None
        if registry is None or any(
            host != registry.registry or repo not in registry.repositories for host, repo in found
        ):
            raise EcrPullError("IMAGE_REGISTRY_NOT_ALLOWED")
        return registry.registry


@dataclass(frozen=True)
class PullCredential:
    registry: str
    username: str
    expires_at: int  # epoch seconds
    _password: str = field(repr=False)

    def reveal(self) -> str:
        return self._password

    def __str__(self) -> str:
        return f"PullCredential(registry={self.registry}, expires_at={self.expires_at})"

    def response(self) -> dict:
        """Agent API body (ticket 29 I3). Sent only to the authenticated Agent."""
        return {
            "registry": self.registry,
            "username": self.username,
            "password": self._password,
            "expires_at": self.expires_at,
        }


def dockerconfigjson(registry: str, username: str, password: str) -> str:
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    entry = {"username": username, "password": password, "auth": auth}
    return json.dumps({"auths": {registry: entry}}, separators=(",", ":"))


class EcrTokenMinter:
    """`client_factory(region)` returns an ECR client for the platform principal."""

    def __init__(self, client_factory: Callable[[str], Any]):
        self._client_factory = client_factory

    def mint(self, registry: PlatformRegistry) -> PullCredential:
        try:
            response = self._client_factory(registry.region).get_authorization_token()
            data = response["authorizationData"][0]
            endpoint = data["proxyEndpoint"]
            username, _, password = (
                base64.b64decode(data["authorizationToken"]).decode().partition(":")
            )
            expires_at = int(data["expiresAt"].timestamp())
        except Exception:  # noqa: BLE001 - AWS messages and tokens never leave this module
            raise EcrPullError("ECR_TOKEN_UNAVAILABLE") from None
        if endpoint.rstrip("/") != f"https://{registry.registry}":
            raise EcrPullError("ECR_ACCOUNT_MISMATCH")  # platform account only
        if not username or not password:
            raise EcrPullError("ECR_TOKEN_UNAVAILABLE")
        return PullCredential(registry.registry, username, expires_at, password)


def boto3_minter(profile: str | None) -> EcrTokenMinter:
    """Platform principal: named profile locally, the instance role on the demo host."""
    import boto3

    def factory(region: str):
        session = boto3.Session(profile_name=profile, region_name=region)
        return session.client("ecr", region_name=region)

    return EcrTokenMinter(factory)
