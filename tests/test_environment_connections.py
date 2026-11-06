"""Ticket 23 (#118) seam: FastAPI TestClient + SQLite business DB + an injected cloud connector.

STS/EKS are botocore clients wrapped in `Stubber`, so no request leaves the process and the
exact AssumeRole parameters (External ID included) are asserted. GCP verification goes through an
injected checker (the real one asks the provisioning worker, ticket 48). Real AWS/GCP accounts are out of scope (ADR-005).
"""

import configparser
import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import ANY, Stubber
from cmp_api import connections
from cmp_api.config import settings
from cmp_api.connections import AwsConnector, ConnectionFailure
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.routes.agents import get_session
from cmp_api.routes.connections import get_aws_connector, get_gcp_checker
from cmp_api.tunnels import TunnelStatus, register_key
from cmp_scanners.aws_infra import get_session as scanner_session
from fastapi.testclient import TestClient
from onprem_rollout_support import register_agent
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ADMIN = {"X-Enrollment-Admin-Key": "operator-only-test-key"}
PLATFORM = "arn:aws:iam::111111111111:role/cmp-demo-instance"
ROLE = "arn:aws:iam::222222222222:role/cmp-source-reader"
TEMPLATES = "https://cmp-templates.s3.ap-northeast-2.amazonaws.com/connections/v1"
TEMP = {
    "AccessKeyId": "ASIAEXAMPLETEMPORARY",
    "SecretAccessKey": "temporary-secret-never-stored",
    "SessionToken": "temporary-session-token-never-stored",
    "Expiration": datetime(2030, 1, 1, tzinfo=UTC),
}


def _client(service):
    return boto3.client(
        service,
        region_name="ap-northeast-2",
        aws_access_key_id="x",
        aws_secret_access_key="x",
    )


class FakeConnector(AwsConnector):
    """Real botocore clients with Stubber; records which credentials each client was built with."""

    def __init__(self):
        self.platform_sts = _client("sts")
        self.assumed_sts = _client("sts")
        self.eks = _client("eks")
        self.stubs = {
            name: Stubber(getattr(self, name)) for name in ("platform_sts", "assumed_sts", "eks")
        }
        for stub in self.stubs.values():
            stub.activate()
        self.assumed_credentials = []

    def platform_client(self, service, region):
        assert service == "sts"
        return self.platform_sts

    def assumed_client(self, service, region, credentials):
        self.assumed_credentials.append(credentials)
        return {"sts": self.assumed_sts, "eks": self.eks}[service]

    def expect_assume(self, external_id, role=ROLE, error=None):
        params = {
            "RoleArn": role,
            "RoleSessionName": ANY,
            "ExternalId": external_id,
            "DurationSeconds": 900,
        }
        if error:
            self.stubs["platform_sts"].add_client_error(
                "assume_role",
                service_error_code=error,
                http_status_code=403,
                expected_params=params,
            )
            return
        self.stubs["platform_sts"].add_response(
            "assume_role",
            {
                "Credentials": TEMP,
                "AssumedRoleUser": {
                    "AssumedRoleId": "AROAEXAMPLE:cmp-verify",
                    "Arn": "arn:aws:sts::222222222222:assumed-role/cmp-source-reader/cmp-verify",
                },
            },
            params,
        )

    def expect_identity(self, account="222222222222"):
        self.stubs["assumed_sts"].add_response(
            "get_caller_identity",
            {
                "UserId": "AROAEXAMPLE:cmp-verify",
                "Account": account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/cmp-source-reader/cmp-verify",
            },
            {},
        )

    def expect_clusters(self, pages=(["shop-prod"],), error=None):
        if error:
            self.stubs["eks"].add_client_error("list_clusters", service_error_code=error)
            return
        for index, names in enumerate(pages):
            params = {"nextToken": f"t{index}"} if index else {}
            body = {"clusters": list(names)}
            if index + 1 < len(pages):
                body["nextToken"] = f"t{index + 1}"
            self.stubs["eks"].add_response("list_clusters", body, params)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def aws():
    return FakeConnector()


@pytest.fixture
def adc_calls():
    return []


@pytest.fixture
def client(engine, aws, adc_calls, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "agent_enrollment_admin_key", "operator-only-test-key")
    monkeypatch.setattr(settings, "connection_platform_principal_arn", PLATFORM)
    monkeypatch.setattr(settings, "connection_template_base_url", TEMPLATES)
    monkeypatch.setattr(settings, "connection_aws_config_file", str(tmp_path / "aws" / "config"))
    base = tmp_path / "base-config"
    base.write_text("[profile cmp-platform]\ncredential_source = Ec2InstanceMetadata\n")
    monkeypatch.setattr(settings, "connection_aws_base_config_file", str(base))
    monkeypatch.setattr(settings, "connection_platform_aws_profile", "cmp-platform")
    monkeypatch.setattr(settings, "connection_gcp_project_id", "cmp-demo-project")
    session = Session(engine)

    def checker(expected):
        adc_calls.append(expected)
        if expected != "cmp-provisioner@cmp-demo-project.iam.gserviceaccount.com":
            raise ConnectionFailure("ADC_PRINCIPAL_MISMATCH")
        return expected

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_aws_connector] = lambda: aws
    app.dependency_overrides[get_gcp_checker] = lambda: checker
    try:
        with TestClient(app) as test_client:
            test_client.session = session
            yield test_client
    finally:
        app.dependency_overrides.clear()
        session.close()


def create(client, kind="AWS_SOURCE", **extra):
    body = {"kind": kind, "display_name": "shop prod", "region": "ap-northeast-2", **extra}
    return client.post("/connections", json=body, headers=ADMIN)


def aws_source(client, role=ROLE):
    created = create(client).json()
    assert (
        client.put(
            f"/connections/{created['connection_id']}/role", json={"role_arn": role}, headers=ADMIN
        ).status_code
        == 200
    )
    return created


# ------------------------------------------------------------------------- access control


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/connections"),
        ("post", "/connections"),
        ("get", "/connections/conn-x"),
        ("put", "/connections/conn-x/role"),
        ("post", "/connections/conn-x/verify"),
        ("delete", "/connections/conn-x"),
        ("get", "/connections/conn-x/eks-clusters"),
        ("get", "/agent-admin/agents"),
    ],
)
def test_every_route_requires_the_operator_key(client, method, path):
    assert getattr(client, method)(path).status_code == 403
    assert (
        getattr(client, method)(path, headers={"X-Enrollment-Admin-Key": "wrong"}).status_code
        == 403
    )


def test_connections_are_unavailable_without_a_configured_operator_key(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_enrollment_admin_key", None)
    assert client.get("/connections", headers=ADMIN).status_code == 403


# ------------------------------------------------------------------------- AWS registration


def test_aws_create_issues_external_id_and_quick_create_link(client):
    response = create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "AWS_SOURCE" and body["status"] == "PENDING"
    assert body["connection_id"].startswith("conn-")
    assert body["external_id"].startswith("cmp-") and len(body["external_id"]) >= 32
    url = body["launch_stack_url"]
    assert url.startswith("https://ap-northeast-2.console.aws.amazon.com/cloudformation/home")
    assert "#/stacks/quickcreate?" in url
    assert "templateURL=https%3A%2F%2Fcmp-templates.s3" in url and "source-role.yaml" in url
    assert f"param_ExternalId={body['external_id']}" in url
    assert "param_PlatformPrincipalArn=arn%3Aaws%3Aiam%3A%3A111111111111" in url


def test_target_uses_the_target_template_and_external_ids_are_unique(client):
    first, second = create(client, kind="AWS_TARGET").json(), create(client).json()
    assert "target-role.yaml" in first["launch_stack_url"]
    assert first["external_id"] != second["external_id"]


def test_no_launch_link_until_template_hosting_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "connection_template_base_url", None)
    body = create(client).json()
    assert body["launch_stack_url"] is None and body["external_id"]


@pytest.mark.parametrize(
    "extra",
    [
        {"aws_access_key_id": "AKIAABCDEFGHIJKLMNOP"},
        {"aws_secret_access_key": "x"},
        {"credentials": {"x": "y"}},
        {"role_arn": ROLE},
        {"external_id": "chosen-by-user"},
    ],
)
def test_aws_create_never_accepts_keys_or_caller_chosen_secrets(client, extra):
    response = create(client, **extra)
    assert response.status_code == 422
    assert "AKIA" not in response.text


@pytest.mark.parametrize("region", ["", "Seoul", "ap-northeast-2; rm", "us-east-1a"])
def test_invalid_region_is_rejected(client, region):
    assert create(client, region=region).status_code == 422


@pytest.mark.parametrize(
    "role",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "arn:aws:iam::12345:role/x",
        "arn:aws:iam::222222222222:user/admin",
        "arn:aws:sts::222222222222:assumed-role/x/y",
        "arn:aws:iam::222222222222:role/x\n[profile evil]",
    ],
)
def test_role_arn_format_is_validated(client, role):
    created = create(client).json()
    response = client.put(
        f"/connections/{created['connection_id']}/role", json={"role_arn": role}, headers=ADMIN
    )
    assert response.status_code == 422


def test_role_is_write_once(client):
    created = aws_source(client)
    path = f"/connections/{created['connection_id']}/role"
    assert client.put(path, json={"role_arn": ROLE}, headers=ADMIN).status_code == 200
    other = "arn:aws:iam::222222222222:role/other"
    response = client.put(path, json={"role_arn": other}, headers=ADMIN)
    assert response.status_code == 409 and response.json()["detail"] == "ROLE_ALREADY_SET"


def test_role_on_gcp_connection_is_refused(client):
    created = create(client, kind="GCP_TARGET", region="asia-northeast3", gcp=GCP).json()
    response = client.put(
        f"/connections/{created['connection_id']}/role", json={"role_arn": ROLE}, headers=ADMIN
    )
    assert response.status_code == 409 and response.json()["detail"] == "ROLE_NOT_APPLICABLE"


# ------------------------------------------------------------------------- AWS verify


def test_verify_before_role_registration_fails_with_role_not_set(client, aws):
    created = create(client).json()
    response = client.post(f"/connections/{created['connection_id']}/verify", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED" and response.json()["code"] == "ROLE_NOT_SET"
    aws.stubs["platform_sts"].assert_no_pending_responses()


def test_source_verify_assumes_with_external_id_and_lists_clusters(client, aws):
    created = aws_source(client)
    aws.expect_assume(created["external_id"])
    aws.expect_identity()
    aws.expect_clusters(pages=(["shop-prod"], ["shop-stage"]))

    response = client.post(f"/connections/{created['connection_id']}/verify", headers=ADMIN)

    body = response.json()
    assert body["status"] == "CONNECTED" and body["code"] == "OK"
    assert body["account_id"] == "222222222222"
    assert body["principal"].startswith("arn:aws:sts::222222222222:assumed-role/")
    assert body["eks_clusters"] == ["shop-prod", "shop-stage"]
    assert isinstance(body["checked_at"], int)
    for stub in aws.stubs.values():
        stub.assert_no_pending_responses()
    assert all(c["SessionToken"] == TEMP["SessionToken"] for c in aws.assumed_credentials)
    detail = client.get(f"/connections/{created['connection_id']}", headers=ADMIN).json()
    assert detail["status"] == "CONNECTED" and detail["account_id"] == "222222222222"
    assert detail["last_verified_at"] == body["checked_at"]


def test_target_verify_skips_cluster_listing(client, aws):
    created = create(client, kind="AWS_TARGET").json()
    client.put(
        f"/connections/{created['connection_id']}/role", json={"role_arn": ROLE}, headers=ADMIN
    )
    aws.expect_assume(created["external_id"])
    aws.expect_identity()
    body = client.post(f"/connections/{created['connection_id']}/verify", headers=ADMIN).json()
    assert body["status"] == "CONNECTED" and "eks_clusters" not in body


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda aws, ext: aws.expect_assume(ext, error="AccessDenied"), "ASSUME_ROLE_DENIED"),
        (
            lambda aws, ext: aws.expect_assume(ext, error="RegionDisabledException"),
            "ASSUME_ROLE_FAILED",
        ),
        (
            lambda aws, ext: (aws.expect_assume(ext), aws.expect_identity(account="333333333333")),
            "ACCOUNT_MISMATCH",
        ),
        (
            lambda aws, ext: (
                aws.expect_assume(ext),
                aws.expect_identity(),
                aws.expect_clusters(error="AccessDeniedException"),
            ),
            "EKS_LIST_DENIED",
        ),
    ],
)
def test_verify_failures_have_stable_codes_and_no_aws_message(client, aws, setup, code):
    created = aws_source(client)
    setup(aws, created["external_id"])
    response = client.post(f"/connections/{created['connection_id']}/verify", headers=ADMIN)
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "FAILED" and body["code"] == code
    detail = client.get(f"/connections/{created['connection_id']}", headers=ADMIN).json()
    assert detail["status"] == "FAILED" and detail["last_code"] == code
    assert detail["last_verified_at"] is None


def test_platform_credentials_missing_is_reported_without_details(client, aws, monkeypatch):
    created = aws_source(client)

    def broken(service, region):
        raise connections.NoCredentialsError()

    monkeypatch.setattr(aws, "platform_client", broken)
    body = client.post(f"/connections/{created['connection_id']}/verify", headers=ADMIN).json()
    assert body["code"] == "PLATFORM_CREDENTIALS_UNAVAILABLE"


def test_temporary_credentials_never_reach_db_responses_or_logs(client, aws, engine, caplog):
    created = aws_source(client)
    aws.expect_assume(created["external_id"])
    aws.expect_identity()
    aws.expect_clusters()
    texts = [client.post(f"/connections/{created['connection_id']}/verify", headers=ADMIN).text]
    texts.append(client.get("/connections", headers=ADMIN).text)
    texts.append(client.get(f"/connections/{created['connection_id']}", headers=ADMIN).text)
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT * FROM environment_connections").fetchall()
    texts.append(json.dumps([list(map(str, row)) for row in rows]))
    texts.append(caplog.text)
    for secret in (TEMP["AccessKeyId"], TEMP["SecretAccessKey"], TEMP["SessionToken"]):
        assert all(secret not in text for text in texts)


# ------------------------------------------------------------------------- anchor discovery


def test_eks_cluster_discovery_for_a_chosen_region(client, aws):
    created = aws_source(client)
    aws.expect_assume(created["external_id"])
    aws.expect_clusters(pages=(["a", "b"],))
    response = client.get(
        f"/connections/{created['connection_id']}/eks-clusters",
        params={"region": "us-west-2"},
        headers=ADMIN,
    )
    assert response.status_code == 200
    assert response.json() == {
        "connection_id": created["connection_id"],
        "region": "us-west-2",
        "eks_clusters": ["a", "b"],
    }


def test_discovery_is_only_for_source_connections_with_a_role(client):
    target = create(client, kind="AWS_TARGET").json()
    path = f"/connections/{target['connection_id']}/eks-clusters"
    assert client.get(path, headers=ADMIN).json()["detail"] == "SOURCE_CONNECTION_REQUIRED"
    source = create(client).json()
    path = f"/connections/{source['connection_id']}/eks-clusters"
    response = client.get(path, headers=ADMIN)
    assert response.status_code == 409 and response.json()["detail"] == "ROLE_NOT_SET"
    assert client.get(path, params={"region": "bad"}, headers=ADMIN).status_code == 422


# ------------------------------------------------------------------------- GCP

GCP = {
    "project_id": "cmp-demo-project",
    "expected_principal": "cmp-provisioner@cmp-demo-project.iam.gserviceaccount.com",
}


def test_gcp_target_is_verified_with_the_adc_principal_check(client, adc_calls):
    created = create(client, kind="GCP_TARGET", region="asia-northeast3", gcp=GCP)
    assert created.status_code == 201
    body = created.json()
    assert "external_id" not in body or body["external_id"] is None
    assert body.get("launch_stack_url") is None
    result = client.post(f"/connections/{body['connection_id']}/verify", headers=ADMIN).json()
    assert result["status"] == "CONNECTED" and result["project_id"] == "cmp-demo-project"
    assert result["principal"] == GCP["expected_principal"]
    assert adc_calls == [GCP["expected_principal"]]


def test_gcp_principal_mismatch_fails(client):
    other = {**GCP, "expected_principal": "someone@cmp-demo-project.iam.gserviceaccount.com"}
    body = create(client, kind="GCP_TARGET", region="asia-northeast3", gcp=other).json()
    result = client.post(f"/connections/{body['connection_id']}/verify", headers=ADMIN).json()
    assert result["status"] == "FAILED" and result["code"] == "ADC_PRINCIPAL_MISMATCH"


@pytest.mark.parametrize(
    "gcp",
    [
        None,
        {**GCP, "project_id": "another-project"},
        {**GCP, "service_account_key": '{"type": "service_account"}'},
        {**GCP, "expected_principal": "not-an-email"},
    ],
)
def test_gcp_rejects_other_projects_missing_fields_and_key_json(client, gcp):
    extra = {"gcp": gcp} if gcp is not None else {}
    response = create(client, kind="GCP_TARGET", region="asia-northeast3", **extra)
    assert response.status_code == 422


def test_gcp_fields_are_refused_on_aws_connections(client):
    assert create(client, gcp=GCP).status_code == 422


# ------------------------------------------------------------------------- unified list / delete


def test_unified_list_matches_the_web_contract(client, aws, monkeypatch):
    now = int(datetime.now(UTC).timestamp())
    source = aws_source(client)
    gcp = create(client, kind="GCP_TARGET", region="asia-northeast3", gcp=GCP).json()
    s = client.session
    register_agent(s, agent_id="agent-online", cluster_uid="uid-1")
    register_agent(s, agent_id="agent-stale", cluster_uid="uid-2")
    agents = {a.id: a for a in s.query(connections.RegisteredAgent)}
    agents["agent-online"].last_seen_at = now - 10
    agents["agent-stale"].last_seen_at = now - 61
    tunnel = register_key(s, "agent-online", "A" * 43 + "=")
    s.add(
        TunnelStatus(
            agent_id="agent-online",
            tunnel_ip=tunnel.tunnel_ip,
            last_handshake_at=now - 5,
            app_health_ok=True,
            db_connect_ok=False,
            reported_at=now,
        )
    )
    s.commit()

    body = client.get("/connections", headers=ADMIN).json()

    assert set(body) == {"connections", "agents"}
    by_id = {c["connection_id"]: c for c in body["connections"]}
    assert by_id[source["connection_id"]] == {
        "connection_id": source["connection_id"],
        "kind": "AWS_SOURCE",
        "display_name": "shop prod",
        "account_id": None,
        "region": "ap-northeast-2",
        "status": "PENDING",
        "last_verified_at": None,
    }
    assert by_id[gcp["connection_id"]] == {
        "connection_id": gcp["connection_id"],
        "kind": "GCP_TARGET",
        "display_name": "shop prod",
        "project_id": "cmp-demo-project",
        "region": "asia-northeast3",
        "status": "PENDING",
        "last_verified_at": None,
    }
    agents_by_id = {a["agent_id"]: a for a in body["agents"]}
    assert agents_by_id["agent-online"] == {
        "agent_id": "agent-online",
        "cluster_uid": "uid-1",
        "last_seen_at": now - 10,
        "online": True,
        "revoked": False,
        "version": None,
        "tunnel": {
            "tunnel_ip": tunnel.tunnel_ip,
            "last_handshake_at": now - 5,
            "app_health_ok": True,
            "db_connect_ok": False,
            "stale": False,
            "fresh_until": now + 60,
        },
    }
    assert agents_by_id["agent-stale"]["online"] is False
    assert agents_by_id["agent-stale"]["tunnel"] is None
    assert client.get("/agent-admin/agents", headers=ADMIN).json() == {"agents": body["agents"]}


def test_agent_tunnel_marks_stale_reports_without_changing_online(client):
    """Ticket 25 F6: `tunnel.stale` follows report/handshake age; `online` stays as in 23."""
    s = client.session
    register_agent(s, agent_id="agent-001", cluster_uid="uid-1")
    s.query(connections.RegisteredAgent).one().last_seen_at = 1000
    tunnel = register_key(s, "agent-001", "A" * 43 + "=")
    status = TunnelStatus(
        agent_id="agent-001",
        tunnel_ip=tunnel.tunnel_ip,
        last_handshake_at=990,
        app_health_ok=True,
        db_connect_ok=True,
        reported_at=1000,
    )
    s.add(status)
    s.commit()

    def summary(now):
        (item,) = connections.agent_summaries(s, now=now)
        return item

    fresh = summary(1060)
    assert (fresh["online"], fresh["tunnel"]["stale"], fresh["tunnel"]["fresh_until"]) == (
        True,
        False,
        1060,
    )
    assert summary(1061)["tunnel"]["stale"] is True
    assert summary(1061)["online"] is False  # 23 rule (last_seen_at + 60), unchanged
    status.last_handshake_at = 800  # handshake 200 s old although the report is recent
    s.commit()
    assert summary(1000)["tunnel"] | {"stale": True, "fresh_until": 980} == summary(1000)["tunnel"]
    status.last_handshake_at = None
    s.commit()
    assert summary(1000)["tunnel"]["stale"] is True
    assert summary(1000)["tunnel"]["fresh_until"] is None


def test_delete_removes_platform_data_and_points_at_the_stack(client):
    created = aws_source(client)
    connection_id = created["connection_id"]
    response = client.delete(f"/connections/{connection_id}", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True and "CloudFormation" in body["notice"]
    assert body["stack_name"].startswith("cmp-")
    assert client.get(f"/connections/{connection_id}", headers=ADMIN).status_code == 404
    assert client.delete(f"/connections/{connection_id}", headers=ADMIN).status_code == 404


# ------------------------------------------------------------------------- credential_ref


def read_profiles(path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(path)
    return parser


def test_role_registration_renders_an_assume_role_profile_for_existing_code(client, monkeypatch):
    created = aws_source(client)
    detail = client.get(f"/connections/{created['connection_id']}", headers=ADMIN).json()
    ref = detail["credential_ref"]
    assert ref == f"cmp-conn-{created['connection_id'].removeprefix('conn-')}"

    profiles = read_profiles(settings.connection_aws_config_file)
    assert profiles["profile cmp-platform"]["credential_source"] == "Ec2InstanceMetadata"
    section = profiles[f"profile {ref}"]
    assert section["role_arn"] == ROLE
    assert section["external_id"] == created["external_id"]
    assert section["source_profile"] == "cmp-platform"
    assert section["region"] == "ap-northeast-2"
    assert "aws_access_key_id" not in section and "aws_session_token" not in section

    # Analysis `get_session` and Provisioning `aws_profile` resolve the same named profile.
    monkeypatch.setenv("AWS_CONFIG_FILE", settings.connection_aws_config_file)
    resolved = scanner_session(ref, region="ap-northeast-2")._session.get_scoped_config()
    assert resolved["role_arn"] == ROLE and resolved["external_id"] == created["external_id"]

    client.delete(f"/connections/{created['connection_id']}", headers=ADMIN)
    assert f"profile {ref}" not in read_profiles(settings.connection_aws_config_file)


def test_gcp_and_pending_connections_have_no_aws_profile(client):
    pending = create(client).json()
    gcp = create(client, kind="GCP_TARGET", region="asia-northeast3", gcp=GCP).json()
    for item in (pending, gcp):
        detail = client.get(f"/connections/{item['connection_id']}", headers=ADMIN).json()
        assert detail["credential_ref"] is None


def test_external_id_is_shown_only_to_the_operator_detail_not_the_list(client):
    created = create(client).json()
    listing = client.get("/connections", headers=ADMIN).text
    assert created["external_id"] not in listing
    detail = client.get(f"/connections/{created['connection_id']}", headers=ADMIN).json()
    assert detail["external_id"] == created["external_id"]


def test_worker_gcp_check_reuses_the_planning_adc_check(monkeypatch):
    # Ticket 48 G7: the check moved to the provisioning worker Activity (the API has no ADC).
    import asyncio

    from cmp_providers import gcp_preview
    from cmp_worker.activities.gcp_connection import verify_gcp_connection

    seen = []

    def fake(expected, work, env):
        seen.append(expected)
        if expected.startswith("bad"):
            gcp_preview._reject("ADC_PRINCIPAL_MISMATCH", "adc", "mismatch")

    monkeypatch.setattr(gcp_preview, "_verify_adc", fake)
    ok = asyncio.run(verify_gcp_connection("ok@p.iam.gserviceaccount.com"))
    assert ok == {"code": "OK", "principal": "ok@p.iam.gserviceaccount.com"}
    bad = asyncio.run(verify_gcp_connection("bad@p.iam.gserviceaccount.com"))
    assert bad == {"code": "ADC_PRINCIPAL_MISMATCH", "principal": None}
    assert seen == ["ok@p.iam.gserviceaccount.com", "bad@p.iam.gserviceaccount.com"]
