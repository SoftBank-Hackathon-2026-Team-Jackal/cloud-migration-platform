"""Ticket 48 (#183) G5/G6: ADC principal check for service-account key, WIF and user ADC.

Fake `gcloud` on PATH records its arguments and prints a token; the fake tokeninfo returns an
`email` only for a token minted with the userinfo.email scope, like Google does for service
accounts.
"""

import json
from pathlib import Path

import pytest
from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers import gcp_adc
from cmp_providers.gcp_apply import default_adc_email
from cmp_providers.gcp_preview import verify_adc_principal

SA = "cmp-provisioning@sbh26-prod.iam.gserviceaccount.com"
OTHER = "someone-else@sbh26-prod.iam.gserviceaccount.com"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
IMPERSONATION = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{}:generateAccessToken"
)


@pytest.fixture
def gcloud(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "gcloud-calls"
    script = tools / "gcloud"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ -n "$FAKE_GCLOUD_FAIL" ]; then exit 1; fi\n'
        'case "$*" in *userinfo.email*) printf "%s" "token-with-email";; '
        '*) printf "%s" "token-cloud-platform";; esac\n'
    )
    script.chmod(0o755)
    config = tmp_path / "gcloud-config"
    config.mkdir()
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(config))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    return config, calls


def tokeninfo(monkeypatch, *, email=SA, user_email=None):
    """Google tokeninfo: service-account tokens carry `email` only with the email scope."""

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.body).encode()

    def urlopen(request, timeout):
        token = dict(pair.split("=", 1) for pair in request.data.decode().split("&"))[
            "access_token"
        ]
        body = {"azp": "1234567890", "scope": "https://www.googleapis.com/auth/cloud-platform"}
        if token == "token-with-email" and email:
            body["email"] = email
        if user_email:
            body["email"] = user_email
        return Response(body)

    monkeypatch.setattr("cmp_providers.gcp_adc.urllib.request.urlopen", urlopen)


def write_adc(config: Path, body: dict) -> None:
    (config / "application_default_credentials.json").write_text(json.dumps(body))


def service_account_key(email=SA):
    # Shape of a key file; the private key is a placeholder, never a real secret.
    return {"type": "service_account", "client_email": email, "private_key": "PLACEHOLDER"}


def wif_config(email=SA):
    return {
        "type": "external_account",
        "audience": "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/aws",
        "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
        "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {"environment_id": "aws1"},
        "service_account_impersonation_url": IMPERSONATION.format(email),
    }


def test_service_account_key_requests_the_email_scope_and_matches_client_email(gcloud, monkeypatch):
    config, calls = gcloud
    write_adc(config, service_account_key())
    tokeninfo(monkeypatch)
    assert verify_adc_principal(SA) == SA
    assert EMAIL_SCOPE in calls.read_text()
    with pytest.raises(ProvisioningInputError, match="ADC_PRINCIPAL_MISMATCH"):
        verify_adc_principal(OTHER)


def test_service_account_token_for_another_identity_is_refused(gcloud, monkeypatch):
    config, _ = gcloud
    write_adc(config, service_account_key())
    tokeninfo(monkeypatch, email=OTHER)  # the token does not belong to the file's account
    with pytest.raises(ProvisioningInputError, match="ADC_PRINCIPAL_MISMATCH"):
        verify_adc_principal(SA)


def test_workload_identity_federation_uses_the_impersonated_service_account(gcloud, monkeypatch):
    config, calls = gcloud
    write_adc(config, wif_config())
    tokeninfo(monkeypatch, email=None)  # impersonated cloud-platform token: no email
    assert verify_adc_principal(SA) == SA
    assert EMAIL_SCOPE not in calls.read_text()  # --scopes is unsupported for external accounts
    with pytest.raises(ProvisioningInputError, match="ADC_PRINCIPAL_MISMATCH"):
        verify_adc_principal(OTHER)


def test_impersonated_token_with_a_different_email_is_refused(gcloud, monkeypatch):
    config, _ = gcloud
    write_adc(config, wif_config())
    tokeninfo(monkeypatch, user_email=OTHER)
    with pytest.raises(ProvisioningInputError, match="ADC_PRINCIPAL_MISMATCH"):
        verify_adc_principal(SA)


def test_user_adc_keeps_comparing_the_tokeninfo_email(gcloud, monkeypatch):
    config, calls = gcloud
    write_adc(config, {"type": "authorized_user", "client_id": "x", "refresh_token": "PLACEHOLDER"})
    tokeninfo(monkeypatch, user_email="planner@example.com")
    assert verify_adc_principal("planner@example.com") == "planner@example.com"
    assert EMAIL_SCOPE not in calls.read_text()


@pytest.mark.parametrize(
    "body",
    [
        {"type": "external_account", "audience": "x", "credential_source": {}},
        {"type": "external_account_authorized_user"},
        {"type": "gdch_service_account"},
    ],
)
def test_unsupported_adc_sources_fail_closed(gcloud, monkeypatch, body):
    config, calls = gcloud
    write_adc(config, body)
    tokeninfo(monkeypatch)
    with pytest.raises(ProvisioningInputError, match="ADC_SOURCE_UNSUPPORTED"):
        verify_adc_principal(SA)
    assert not calls.exists()


def test_broken_adc_file_or_token_failure_is_unavailable(gcloud, monkeypatch):
    config, _ = gcloud
    tokeninfo(monkeypatch)
    (config / "application_default_credentials.json").write_text("{not json")
    with pytest.raises(ProvisioningInputError, match="ADC_UNAVAILABLE"):
        verify_adc_principal(SA)
    write_adc(config, service_account_key())
    monkeypatch.setenv("FAKE_GCLOUD_FAIL", "1")
    with pytest.raises(ProvisioningInputError, match="ADC_UNAVAILABLE"):
        verify_adc_principal(SA)


def test_apply_lookup_uses_the_same_principal_rules(gcloud, monkeypatch):
    config, _ = gcloud
    write_adc(config, service_account_key())
    tokeninfo(monkeypatch)
    import os

    assert default_adc_email(dict(os.environ)) == SA
    write_adc(config, {"type": "gdch_service_account"})
    assert default_adc_email(dict(os.environ)) is None


def test_principal_is_read_without_exposing_key_material(gcloud, monkeypatch):
    config, _ = gcloud
    write_adc(config, service_account_key())
    source = gcp_adc.read_adc_source({"CLOUDSDK_CONFIG": str(config)})
    assert source == gcp_adc.AdcSource("service_account", SA)
    assert "PLACEHOLDER" not in repr(source)
