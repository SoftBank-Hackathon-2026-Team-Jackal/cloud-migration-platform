"""Ticket 56 (#219) seam: FastAPI TestClient + SQLite business DB + injected AWS and Kubernetes.

STS/EKS/ELBv2/IAM are botocore clients wrapped in `Stubber`; the Kubernetes API is a fake built
by the injected `get_kube_factory`, which records every path read. Real AWS/EKS is out of scope.
"""

import base64
import json
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.stub import ANY, Stubber
from cmp_api import scope_selection
from cmp_api.config import settings
from cmp_api.connections import AwsConnector, ConnectionFailure, EnvironmentConnection
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.routes.agents import get_session
from cmp_api.routes.connections import get_aws_connector, get_kube_factory
from cmp_api.scope_selection import KubeNotFound, ScopeAnalysisRecord, ScopeConfirmationRecord
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ADMIN = {"X-Enrollment-Admin-Key": "operator-only-test-key"}
ACCOUNT = "714066696240"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/cmp-source-reader"
REGION = "ap-northeast-2"
CLUSTER = "cmp-demo-source"
SECRET_LITERAL = "literal-password-never-leaves"
CA_PEM = b"-----BEGIN CERTIFICATE-----\nMIIfake\n-----END CERTIFICATE-----\n"
TEMP = {
    "AccessKeyId": "ASIAEXAMPLETEMPORARY",
    "SecretAccessKey": "temporary-secret-never-stored",
    "SessionToken": "temporary-session-token-never-stored",
    "Expiration": datetime(2030, 1, 1, tzinfo=UTC),
}
ALB_DNS = "k8s-chatsour-chatapp-0123456789-1234567890.ap-northeast-2.elb.amazonaws.com"
ALB_ARN = (
    f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/"
    "k8s-chatsour-chatapp-0123456789/50dc6c495c0c9188"
)
IRSA = f"arn:aws:iam::{ACCOUNT}:role/chat-app-irsa"


def _client(service):
    return boto3.client(
        service, region_name=REGION, aws_access_key_id="x", aws_secret_access_key="x"
    )


class FakeConnector(AwsConnector):
    def __init__(self):
        self.clients = {
            "platform_sts": _client("sts"),
            "eks": _client("eks"),
            "elbv2": _client("elbv2"),
            "iam": _client("iam"),
        }
        # Presigning is local: a real client with fake credentials, never stubbed or sent.
        self.token_sts = boto3.client(
            "sts",
            region_name=REGION,
            aws_access_key_id=TEMP["AccessKeyId"],
            aws_secret_access_key=TEMP["SecretAccessKey"],
            aws_session_token=TEMP["SessionToken"],
        )
        self.stubs = {name: Stubber(client) for name, client in self.clients.items()}
        for stub in self.stubs.values():
            stub.activate()

    def platform_client(self, service, region):
        assert service == "sts"
        return self.clients["platform_sts"]

    def assumed_client(self, service, region, credentials):
        assert credentials["AccessKeyId"] == TEMP["AccessKeyId"]
        return self.token_sts if service == "sts" else self.clients[service]

    def expect_assume(self):
        self.stubs["platform_sts"].add_response(
            "assume_role",
            {
                "Credentials": TEMP,
                "AssumedRoleUser": {"AssumedRoleId": "AROAX:cmp", "Arn": f"{ROLE}/cmp"},
            },
            {"RoleArn": ROLE, "RoleSessionName": ANY, "ExternalId": ANY, "DurationSeconds": 900},
        )

    def expect_cluster(self, name=CLUSTER, error=None):
        if error:
            self.stubs["eks"].add_client_error(
                "describe_cluster", service_error_code=error, expected_params={"name": name}
            )
            return
        self.stubs["eks"].add_response(
            "describe_cluster",
            {
                "cluster": {
                    "name": name,
                    "endpoint": "https://ABCDEF.gr7.ap-northeast-2.eks.amazonaws.com",
                    "certificateAuthority": {"data": base64.b64encode(CA_PEM).decode()},
                }
            },
            {"name": name},
        )

    def expect_load_balancers(self):
        self.stubs["elbv2"].add_response(
            "describe_load_balancers",
            {
                "LoadBalancers": [
                    {"LoadBalancerArn": "arn:other", "DNSName": "other.elb.amazonaws.com"},
                    {"LoadBalancerArn": ALB_ARN, "DNSName": ALB_DNS},
                ]
            },
            {},
        )

    def expect_irsa_policies(self):
        iam = self.stubs["iam"]
        inline = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject"],
                    "Resource": "arn:aws:s3:::chat-attachments/*",
                },
                {"Effect": "Allow", "Action": "s3:ListAllMyBuckets", "Resource": "*"},
                {
                    "Effect": "Deny",
                    "Action": "sqs:*",
                    "Resource": f"arn:aws:sqs:{REGION}:{ACCOUNT}:denied-queue",
                },
            ],
        }
        iam.add_response(
            "list_role_policies", {"PolicyNames": ["chat-s3"]}, {"RoleName": "chat-app-irsa"}
        )
        iam.add_response(
            "get_role_policy",
            {
                "RoleName": "chat-app-irsa",
                "PolicyName": "chat-s3",
                "PolicyDocument": json.dumps(inline),
            },
            {"RoleName": "chat-app-irsa", "PolicyName": "chat-s3"},
        )
        policy_arn = f"arn:aws:iam::{ACCOUNT}:policy/chat-events"
        iam.add_response(
            "list_attached_role_policies",
            {"AttachedPolicies": [{"PolicyName": "chat-events", "PolicyArn": policy_arn}]},
            {"RoleName": "chat-app-irsa"},
        )
        iam.add_response(
            "get_policy",
            {"Policy": {"PolicyName": "chat-events", "Arn": policy_arn, "DefaultVersionId": "v3"}},
            {"PolicyArn": policy_arn},
        )
        managed = {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sqs:SendMessage",
                "Resource": f"arn:aws:sqs:{REGION}:{ACCOUNT}:chat-events",
            },
        }
        iam.add_response(
            "get_policy_version",
            {"PolicyVersion": {"Document": json.dumps(managed), "VersionId": "v3"}},
            {"PolicyArn": policy_arn, "VersionId": "v3"},
        )


def _meta(name, **extra):
    return {"metadata": {"name": name, **extra}}


def _pod_template(labels, containers, service_account=None):
    spec = {"containers": containers}
    if service_account:
        spec["serviceAccountName"] = service_account
    return {"metadata": {"labels": labels}, "spec": spec}


APP_CONTAINER = {
    "name": "app",
    "image": "123.dkr.ecr.ap-northeast-2.amazonaws.com/chat-app:1",
    "env": [
        {"name": "ADMIN_PASSWORD", "value": SECRET_LITERAL},
        {
            "name": "DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "chat-db", "key": "url"}},
        },
        {
            "name": "LOG_LEVEL",
            "valueFrom": {"configMapKeyRef": {"name": "chat-config", "key": "log_level"}},
        },
    ],
    "envFrom": [{"configMapRef": {"name": "chat-flags"}}],
}
DB_CONTAINER = {
    "name": "postgres",
    "image": "postgres:16",
    "env": [
        {
            "name": "POSTGRES_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": "chat-db", "key": "password"}},
        }
    ],
}


def chat_source():
    """`chat-source` as in the demo source cluster (plus IRSA, an unrelated StatefulSet)."""
    ns = "/api/v1/namespaces/chat-source"
    apps = "/apis/apps/v1/namespaces/chat-source"
    deployment = {
        **_meta("chat-app", namespace="chat-source"),
        "spec": {"template": _pod_template({"app": "chat-app"}, [APP_CONTAINER], "chat-app")},
    }
    db = {
        **_meta("chat-db", namespace="chat-source"),
        "spec": {"template": _pod_template({"app": "chat-db"}, [DB_CONTAINER])},
    }
    other_db = {
        **_meta("legacy-db", namespace="chat-source"),
        "spec": {
            "template": _pod_template(
                {"app": "legacy"}, [{"name": "pg", "image": "docker.io/bitnami/postgresql:15"}]
            )
        },
    }
    return {
        "/api/v1/namespaces": [
            _meta(name)
            for name in (
                "default",
                "kube-system",
                "kube-public",
                "kube-node-lease",
                "amazon-cloudwatch",
                "aws-observability",
                "gatekeeper-system",
                "chat-source",
            )
        ],
        ns: _meta("chat-source"),
        f"{apps}/deployments": [deployment],
        f"{apps}/statefulsets": [db, other_db],
        f"{apps}/deployments/chat-app": deployment,
        f"{apps}/statefulsets/chat-db": db,
        f"{ns}/serviceaccounts/chat-app": {
            **_meta("chat-app", annotations={"eks.amazonaws.com/role-arn": IRSA})
        },
        f"{ns}/services": [
            {**_meta("chat-app"), "spec": {"selector": {"app": "chat-app"}}},
            {**_meta("chat-db"), "spec": {"selector": {"app": "chat-db"}}},
        ],
        "/apis/networking.k8s.io/v1/namespaces/chat-source/ingresses": [
            {
                **_meta(
                    "chat-app",
                    annotations={"alb.ingress.kubernetes.io/scheme": "internet-facing"},
                ),
                "spec": {
                    "ingressClassName": "alb",
                    "rules": [
                        {
                            "http": {
                                "paths": [
                                    {
                                        "backend": {
                                            "service": {"name": "chat-app", "port": {"number": 80}}
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                },
                "status": {"loadBalancer": {"ingress": [{"hostname": ALB_DNS}]}},
            }
        ],
        "/apis/networking.k8s.io/v1/namespaces/chat-source/networkpolicies": [
            {
                **_meta("chat-app-egress"),
                "spec": {
                    "podSelector": {"matchLabels": {"app": "chat-app"}},
                    "egress": [{"to": [{"podSelector": {"matchLabels": {"app": "chat-db"}}}]}],
                },
            }
        ],
    }


class FakeKube:
    def __init__(self, objects, denied=False):
        self.objects = objects
        self.denied = denied
        self.paths = []

    def _read(self, path):
        self.paths.append(path)
        assert "/secrets" not in path and "/configmaps" not in path
        if self.denied:
            raise ConnectionFailure("EKS_ACCESS_DENIED")
        if path not in self.objects:
            raise KubeNotFound(path)
        return self.objects[path]

    def get(self, path):
        value = self._read(path)
        assert isinstance(value, dict), path
        return value

    def list(self, path):
        try:
            value = self._read(path)
        except KubeNotFound:
            return []
        assert isinstance(value, list), path
        return value


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
def kube():
    return FakeKube(chat_source())


@pytest.fixture
def opened():
    return []


@pytest.fixture
def client(engine, aws, kube, opened, monkeypatch):
    monkeypatch.setattr(settings, "agent_enrollment_admin_key", "operator-only-test-key")
    session = Session(engine)

    def factory(endpoint, ca_pem, token):
        opened.append((endpoint, ca_pem, token))
        return kube

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_aws_connector] = lambda: aws
    app.dependency_overrides[get_kube_factory] = lambda: factory
    try:
        with TestClient(app) as test_client:
            test_client.session = session
            yield test_client
    finally:
        app.dependency_overrides.clear()
        session.close()


def source(client, *, kind="AWS_SOURCE", status="CONNECTED"):
    connection = EnvironmentConnection(
        id="conn-0123456789ab",
        kind=kind,
        display_name="demo source",
        region=REGION,
        external_id="cmp-" + "0" * 32,
        role_arn=ROLE,
        account_id=ACCOUNT,
        status=status,
        created_at=0,
    )
    client.session.add(connection)
    client.session.commit()
    return connection.id


def namespaces_url(cid, cluster=CLUSTER):
    return f"/connections/{cid}/eks-clusters/{cluster}/namespaces"


def workloads_url(cid, ns="chat-source"):
    return f"{namespaces_url(cid)}/{ns}/workloads"


SELECTION = {
    "region": REGION,
    "cluster": CLUSTER,
    "namespace": "chat-source",
    "workload": "chat-app",
}


def analyze(client, cid, aws, **body):
    aws.expect_assume()
    aws.expect_cluster()
    return client.post(
        f"/connections/{cid}/scope-analyses", json={**SELECTION, **body}, headers=ADMIN
    )


def analyze_chat_app(client, aws):
    cid = source(client)
    aws.expect_load_balancers()
    aws.expect_irsa_policies()
    response = analyze(client, cid, aws)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------------- access control


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/connections/conn-x/eks-clusters/c/namespaces"),
        ("get", "/connections/conn-x/eks-clusters/c/namespaces/n/workloads"),
        ("post", "/connections/conn-x/scope-analyses"),
        ("post", "/scope-analyses/sa-x/confirm"),
    ],
)
def test_every_scope_route_requires_the_operator_key(client, method, path):
    assert getattr(client, method)(path).status_code == 403
    assert (
        getattr(client, method)(path, headers={"X-Enrollment-Admin-Key": "no"}).status_code == 403
    )


# ------------------------------------------------------------------------- namespaces


def test_namespaces_exclude_system_namespaces(client, aws, opened):
    cid = source(client)
    aws.expect_assume()
    aws.expect_cluster()
    response = client.get(namespaces_url(cid), params={"region": REGION}, headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {
        "connection_id": cid,
        "region": REGION,
        "cluster": CLUSTER,
        "namespaces": ["chat-source", "default"],
    }
    ((endpoint, ca_pem, token),) = opened
    assert endpoint == "https://ABCDEF.gr7.ap-northeast-2.eks.amazonaws.com"
    assert ca_pem == CA_PEM
    assert token.startswith("k8s-aws-v1.")


def test_eks_token_is_a_presigned_get_caller_identity_bound_to_the_cluster(aws):
    token = scope_selection.eks_token(aws.token_sts, CLUSTER)
    prefix, encoded = token.split(".", 1)
    assert prefix == "k8s-aws-v1" and "=" not in encoded
    url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.hostname.startswith("sts.")
    assert query["Action"] == ["GetCallerIdentity"]
    assert "x-k8s-aws-id" in query["X-Amz-SignedHeaders"][0].split(";")
    assert int(query["X-Amz-Expires"][0]) <= 60
    assert TEMP["SecretAccessKey"] not in url


def test_region_defaults_to_the_connection_region(client, aws):
    cid = source(client)
    aws.expect_assume()
    aws.expect_cluster()
    assert client.get(namespaces_url(cid), headers=ADMIN).json()["region"] == REGION


def test_kubernetes_denial_is_eks_access_denied(client, aws, kube):
    cid = source(client)
    kube.denied = True
    aws.expect_assume()
    aws.expect_cluster()
    response = client.get(namespaces_url(cid), headers=ADMIN)
    assert (response.status_code, response.json()) == (409, {"detail": "EKS_ACCESS_DENIED"})


def test_unknown_cluster_is_cluster_not_found(client, aws):
    cid = source(client)
    aws.expect_assume()
    aws.expect_cluster("ghost", error="ResourceNotFoundException")
    response = client.get(namespaces_url(cid, "ghost"), headers=ADMIN)
    assert (response.status_code, response.json()) == (404, {"detail": "CLUSTER_NOT_FOUND"})


@pytest.mark.parametrize(
    ("kind", "status", "code", "http"),
    [
        ("AWS_TARGET", "CONNECTED", "SOURCE_CONNECTION_REQUIRED", 409),
        ("AWS_SOURCE", "PENDING", "CONNECTION_NOT_READY", 409),
        ("AWS_SOURCE", "FAILED", "CONNECTION_NOT_READY", 409),
    ],
)
def test_connection_must_be_a_verified_source(client, aws, kind, status, code, http):
    cid = source(client, kind=kind, status=status)
    for response in (
        client.get(namespaces_url(cid), headers=ADMIN),
        client.get(workloads_url(cid), headers=ADMIN),
        client.post(f"/connections/{cid}/scope-analyses", json=SELECTION, headers=ADMIN),
    ):
        assert (response.status_code, response.json()) == (http, {"detail": code})


def test_unknown_connection_is_not_found(client):
    response = client.get(namespaces_url("conn-ffffffffffff"), headers=ADMIN)
    assert (response.status_code, response.json()) == (404, {"detail": "CONNECTION_NOT_FOUND"})


def test_invalid_path_names_are_rejected_before_any_call(client):
    cid = source(client)
    assert client.get(namespaces_url(cid, "bad/name"), headers=ADMIN).status_code == 404
    assert client.get(workloads_url(cid, "Bad_NS"), headers=ADMIN).status_code == 422


# ------------------------------------------------------------------------- workloads


def test_workloads_list_deployments_and_statefulsets(client, aws):
    cid = source(client)
    aws.expect_assume()
    aws.expect_cluster()
    response = client.get(workloads_url(cid), params={"region": REGION}, headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {
        "connection_id": cid,
        "region": REGION,
        "cluster": CLUSTER,
        "namespace": "chat-source",
        "workloads": [
            {"kind": "Deployment", "name": "chat-app"},
            {"kind": "StatefulSet", "name": "chat-db"},
            {"kind": "StatefulSet", "name": "legacy-db"},
        ],
    }


@pytest.mark.parametrize("ns", ["missing", "kube-system"])
def test_unknown_or_system_namespace_is_namespace_not_found(client, aws, ns):
    cid = source(client)
    aws.expect_assume()
    aws.expect_cluster()
    response = client.get(workloads_url(cid, ns), headers=ADMIN)
    assert (response.status_code, response.json()) == (404, {"detail": "NAMESPACE_NOT_FOUND"})


# ------------------------------------------------------------------------- analysis


def by_id(analysis):
    return {item["candidate_id"]: item for item in analysis["candidates"]}


def test_analysis_matches_the_web_shape_and_reads_only_real_evidence(client, aws):
    analysis = analyze_chat_app(client, aws)
    assert set(analysis) == {"analysis_id", "selection", "candidates"}
    assert analysis["analysis_id"].startswith("sa-")
    assert analysis["selection"] == {
        "connection_id": "conn-0123456789ab",
        "workload_kind": "Deployment",
        **SELECTION,
    }
    for item in analysis["candidates"]:
        assert set(item) == {
            "candidate_id",
            "resource_type",
            "name",
            "arn",
            "evidence",
            "suggested",
        }
        for evidence in item["evidence"]:
            assert evidence["kind"] in {
                "CODE",
                "MANIFEST",
                "CLOUD_RESOURCE",
                "IAM_POLICY",
                "CLOUDTRAIL",
                "CONFIG",
            }
    candidates = by_id(analysis)
    assert candidates["alb-chat-app"] == {
        "candidate_id": "alb-chat-app",
        "resource_type": "ALB",
        "name": "k8s-chatsour-chatapp-0123456789",
        "arn": ALB_ARN,
        "evidence": [
            {"kind": "MANIFEST", "location": "Ingress chat-app · ingressClassName alb"},
            {"kind": "CLOUD_RESOURCE", "location": f"DescribeLoadBalancers · DNSName {ALB_DNS}"},
        ],
        "suggested": True,
    }
    assert candidates["iam-role-chat-app-irsa"]["arn"] == IRSA
    assert candidates["iam-role-chat-app-irsa"]["evidence"] == [
        {"kind": "MANIFEST", "location": "ServiceAccount chat-app · eks.amazonaws.com/role-arn"}
    ]
    assert candidates["s3-chat-attachments"] == {
        "candidate_id": "s3-chat-attachments",
        "resource_type": "S3",
        "name": "chat-attachments",
        "arn": "arn:aws:s3:::chat-attachments",
        "evidence": [
            {
                "kind": "IAM_POLICY",
                "location": "chat-app-irsa · chat-s3 · s3:GetObject, s3:PutObject",
            }
        ],
        "suggested": True,
    }
    assert candidates["sqs-chat-events"]["arn"] == f"arn:aws:sqs:{REGION}:{ACCOUNT}:chat-events"
    assert candidates["sqs-chat-events"]["evidence"] == [
        {"kind": "IAM_POLICY", "location": "chat-app-irsa · chat-events · sqs:SendMessage"}
    ]
    assert "sqs-denied-queue" not in candidates  # Deny statements are not usage
    assert candidates["secret-chat-db"]["resource_type"] == "SECRET"
    assert candidates["secret-chat-db"]["evidence"] == [
        {
            "kind": "CONFIG",
            "location": "Deployment chat-app env · DATABASE_URL (Secret chat-db key url)",
        }
    ]
    assert candidates["configmap-chat-config"]["evidence"] == [
        {
            "kind": "CONFIG",
            "location": "Deployment chat-app env · LOG_LEVEL (ConfigMap chat-config key log_level)",
        }
    ]
    assert candidates["configmap-chat-flags"]["evidence"] == [
        {"kind": "CONFIG", "location": "Deployment chat-app envFrom · ConfigMap chat-flags"}
    ]
    db = candidates["postgresql-chat-db"]
    assert (db["resource_type"], db["arn"], db["suggested"]) == ("POSTGRESQL", None, True)
    assert db["evidence"] == [
        {"kind": "MANIFEST", "location": "StatefulSet chat-db · image postgres:16"},
        {
            "kind": "MANIFEST",
            "location": "NetworkPolicy chat-app-egress · egress to StatefulSet chat-db",
        },
        {
            "kind": "CONFIG",
            "location": "Secret chat-db referenced by Deployment chat-app and StatefulSet chat-db",
        },
    ]
    legacy = candidates["postgresql-legacy-db"]
    assert legacy["suggested"] is False
    assert legacy["evidence"] == [
        {
            "kind": "MANIFEST",
            "location": "StatefulSet legacy-db · image docker.io/bitnami/postgresql:15",
        }
    ]
    for stub in aws.stubs.values():
        stub.assert_no_pending_responses()


def test_analysis_without_irsa_or_ingress_still_lists_references(client, aws, kube):
    cid = source(client)
    kube.objects.pop("/apis/networking.k8s.io/v1/namespaces/chat-source/ingresses")
    kube.objects["/api/v1/namespaces/chat-source/serviceaccounts/chat-app"] = _meta("chat-app")
    response = analyze(client, cid, aws)
    assert response.status_code == 200
    ids = set(by_id(response.json()))
    assert not any(item.startswith(("alb-", "iam-role-", "s3-", "sqs-")) for item in ids)
    assert {"secret-chat-db", "configmap-chat-config", "postgresql-chat-db"} <= ids


def test_iam_failure_keeps_the_role_and_drops_only_policy_evidence(client, aws):
    cid = source(client)
    aws.expect_load_balancers()
    aws.stubs["iam"].add_client_error("list_role_policies", service_error_code="AccessDenied")
    response = analyze(client, cid, aws)
    assert response.status_code == 200
    candidates = by_id(response.json())
    assert "iam-role-chat-app-irsa" in candidates
    assert not any(key.startswith(("s3-", "sqs-")) for key in candidates)


def test_statefulset_workload_needs_workload_kind_or_falls_back(client, aws):
    cid = source(client)
    response = analyze(client, cid, aws, workload="chat-db")
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["selection"]["workload_kind"] == "StatefulSet"
    assert "postgresql-chat-db" not in by_id(analysis)  # the workload itself is not a candidate


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"workload": "ghost"}, "WORKLOAD_NOT_FOUND"),
        ({"workload": "chat-db", "workload_kind": "Deployment"}, "WORKLOAD_NOT_FOUND"),
        ({"namespace": "missing"}, "NAMESPACE_NOT_FOUND"),
    ],
)
def test_analysis_not_found_codes(client, aws, body, code):
    cid = source(client)
    response = analyze(client, cid, aws, **body)
    assert (response.status_code, response.json()) == (404, {"detail": code})


def test_analysis_rejects_unknown_fields(client):
    cid = source(client)
    response = client.post(
        f"/connections/{cid}/scope-analyses", json={**SELECTION, "extra": 1}, headers=ADMIN
    )
    assert response.status_code == 422


# ------------------------------------------------------------------------- confirmation


def test_confirm_stores_included_and_excluded(client, aws):
    analysis = analyze_chat_app(client, aws)
    ids = [item["candidate_id"] for item in analysis["candidates"]]
    response = client.post(
        f"/scope-analyses/{analysis['analysis_id']}/confirm",
        json={"included": ["alb-chat-app", "s3-chat-attachments", "alb-chat-app"]},
        headers=ADMIN,
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"scope_id", "analysis_id", "included", "excluded", "confirmed_at"}
    assert body["scope_id"].startswith("scope-")
    assert body["analysis_id"] == analysis["analysis_id"]
    assert body["included"] == ["alb-chat-app", "s3-chat-attachments"]
    assert body["excluded"] == [i for i in ids if i not in body["included"]]
    assert datetime.fromisoformat(body["confirmed_at"]).tzinfo is not None
    row = client.session.scalars(select(ScopeConfirmationRecord)).one()
    assert (row.id, row.analysis_id) == (body["scope_id"], analysis["analysis_id"])


@pytest.mark.parametrize("included", [[], ["not-a-candidate"], ["alb-chat-app", "nope"]])
def test_confirm_rejects_invalid_scope(client, aws, included):
    analysis = analyze_chat_app(client, aws)
    response = client.post(
        f"/scope-analyses/{analysis['analysis_id']}/confirm",
        json={"included": included},
        headers=ADMIN,
    )
    assert (response.status_code, response.json()) == (400, {"detail": "INVALID_SCOPE"})


def test_confirm_unknown_analysis(client):
    response = client.post(
        "/scope-analyses/sa-missing/confirm", json={"included": ["x"]}, headers=ADMIN
    )
    assert (response.status_code, response.json()) == (404, {"detail": "ANALYSIS_NOT_FOUND"})


# ------------------------------------------------------------------------- secrets


def test_secret_values_never_leave_the_pod_spec(client, aws, kube, caplog):
    caplog.set_level(logging.DEBUG)
    analysis = analyze_chat_app(client, aws)
    client.post(
        f"/scope-analyses/{analysis['analysis_id']}/confirm",
        json={"included": ["secret-chat-db"]},
        headers=ADMIN,
    )
    assert not any("/secrets" in path or "/configmaps" in path for path in kube.paths)
    assert SECRET_LITERAL not in json.dumps(analysis)
    assert "ADMIN_PASSWORD" not in json.dumps(analysis)  # literal env is not a reference
    stored = client.session.scalars(select(ScopeAnalysisRecord)).one()
    assert SECRET_LITERAL not in json.dumps(
        {column: getattr(stored, column) for column in ("candidates", "selection")}
    )
    assert SECRET_LITERAL not in caplog.text
    # botocore's own DEBUG output prints signed requests; the platform's loggers never do.
    ours = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("botocore"))
    assert TEMP["SecretAccessKey"] not in ours and TEMP["SessionToken"] not in ours


def test_analysis_needs_only_actions_the_source_role_already_grants():
    from pathlib import Path

    import yaml

    template = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "infra/connections/source-role.yaml").read_text()
    )
    (statement,) = template["Resources"]["ReadOnlyScan"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    assert set(scope_selection.REQUIRED_AWS_ACTIONS) <= set(statement["Action"])


# ------------------------------------------------------------------------- real client


def _self_signed_pem() -> bytes:
    from datetime import timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kubernetes")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


def test_real_client_sends_the_bearer_token_pages_and_maps_status(monkeypatch):
    import urllib.error
    import urllib.request

    requests = []
    pages = {
        None: {"items": [_meta("a")], "metadata": {"continue": "next-1"}},
        "next-1": {"items": [_meta("b")], "metadata": {}},
    }

    def urlopen(request, context, timeout):
        requests.append(request)
        query = parse_qs(urlparse(request.full_url).query)
        path = urlparse(request.full_url).path
        if path.endswith("/denied"):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        if path.endswith("/unauthorized"):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        if path.endswith("/missing"):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        if path.endswith("/broken"):
            raise urllib.error.HTTPError(request.full_url, 500, SECRET_LITERAL, {}, None)
        return _Response(pages[query.get("continue", [None])[0]])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    api = scope_selection.EksKubeApi("https://k8s.example/", _self_signed_pem(), "k8s-aws-v1.tok")
    assert [item["metadata"]["name"] for item in api.list("/api/v1/namespaces")] == ["a", "b"]
    assert requests[0].get_method() == "GET"
    assert requests[0].get_header("Authorization") == "Bearer k8s-aws-v1.tok"
    assert requests[0].full_url == "https://k8s.example/api/v1/namespaces?limit=500"
    for path in ("/denied", "/unauthorized"):
        with pytest.raises(ConnectionFailure) as denied:
            api.get(path)
        assert denied.value.code == "EKS_ACCESS_DENIED"
    with pytest.raises(KubeNotFound):
        api.get("/missing")
    with pytest.raises(ConnectionFailure) as broken:
        api.get("/broken")
    assert broken.value.code == "EKS_API_FAILED" and SECRET_LITERAL not in str(broken.value)
