"""Google ADC principal resolution shared by GCP Plan, Apply and connection checks (ticket 48).

The principal comes from the ADC well-known file (`$CLOUDSDK_CONFIG` or `~/.config/gcloud`):

- `service_account` (demo key): the token is minted with the userinfo.email scope, so tokeninfo
  carries `email`; it must equal the file's `client_email`.
- `external_account` / `impersonated_service_account` (Workload Identity Federation, ADR-005):
  the impersonated service account in `service_account_impersonation_url` is the principal. A
  token only exists when that account allowed the exchange; tokeninfo proves it is valid.
- `authorized_user` or no file (GCE metadata): tokeninfo `email`, as before.

Only `type`, `client_email` and the impersonation URL are read; key material, tokens and
command output are never returned or logged.
"""

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_IMPERSONATION = re.compile(r"/serviceAccounts/([^/:]+@[^/:]+):generateAccessToken$")
_IMPERSONATED = ("external_account", "impersonated_service_account")


class AdcSource(NamedTuple):
    kind: str  # service_account | external_account | impersonated_service_account | user
    declared: str | None  # principal named by the credential file, None for user ADC


def _reject(code: str, message: str) -> None:
    raise ProvisioningInputError([ValidationIssue(code=code, path="adc", message=message)])


def adc_file(env: dict[str, str]) -> Path:
    config = env.get("CLOUDSDK_CONFIG") or str(Path(env.get("HOME", "~")) / ".config/gcloud")
    return Path(config).expanduser() / "application_default_credentials.json"


def read_adc_source(env: dict[str, str]) -> AdcSource:
    path = adc_file(env)
    if not path.is_file():
        return AdcSource("user", None)  # gcloud falls back to the metadata server
    try:
        data = json.loads(path.read_text())
        kind = data["type"]
    except (OSError, ValueError, KeyError, TypeError):
        _reject("ADC_UNAVAILABLE", "Google ADC file is unreadable.")
    if kind == "authorized_user":
        return AdcSource("user", None)
    if kind == "service_account" and isinstance(data.get("client_email"), str):
        return AdcSource(kind, data["client_email"])
    if kind in _IMPERSONATED:
        match = _IMPERSONATION.search(str(data.get("service_account_impersonation_url", "")))
        if match:
            return AdcSource(kind, match.group(1))
    _reject("ADC_SOURCE_UNSUPPORTED", "This Google ADC type cannot prove a service principal.")


def token_command(source: AdcSource) -> list[str]:
    command = ["gcloud", "auth", "application-default", "print-access-token"]
    if source.kind == "service_account":
        # Without the email scope a service-account token has no tokeninfo `email` (D-07).
        command.append(f"--scopes={CLOUD_PLATFORM_SCOPE},{EMAIL_SCOPE}")
    return command


def _token(command: list[str], env: dict[str, str], cwd: Path | None) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, check=False, timeout=60
        )
        token = result.stdout.strip().decode("ascii")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        token = ""
    else:
        if result.returncode:
            token = ""
    if not token:
        _reject("ADC_UNAVAILABLE", "Google ADC token is unavailable.")
    return token


def _tokeninfo(token: str) -> dict:
    try:
        body = urllib.parse.urlencode({"access_token": token}).encode()
        request = urllib.request.Request(TOKENINFO_URL, data=body)
        with urllib.request.urlopen(request, timeout=15) as response:
            identity = json.loads(response.read())
    except (OSError, ValueError, UnicodeError, urllib.error.HTTPError):
        identity = None
    if not isinstance(identity, dict):
        _reject("ADC_CHECK_FAILED", "Google ADC identity could not be verified.")
    return identity


def adc_principal(env: dict[str, str], *, cwd: Path | None = None) -> str | None:
    """The verified ADC principal, or None when the token proves no matching identity.

    Raises `ProvisioningInputError` with `ADC_UNAVAILABLE`, `ADC_CHECK_FAILED` or
    `ADC_SOURCE_UNSUPPORTED`.
    """
    source = read_adc_source(env)
    identity = _tokeninfo(_token(token_command(source), env, cwd))
    email = identity.get("email")
    if source.kind == "user":
        return email if isinstance(email, str) else None
    if email is not None and email != source.declared:
        return None
    if source.kind == "service_account" and email is None:
        return None
    return source.declared
