"""Ticket 44: the web start request becomes the real pipeline input (PlanningSettings).

Seam: FastAPI TestClient + faked Temporal start + a temporary `.env` store + SQLite (connections,
Agents) + a temporary target profile catalog. The started Workflow input is then fed to the same
functions the pipeline runs (`PlanningSettings`, `assemble_registration`, Planning registration
and ON_PREM Plan publication), so "accepted" means "usable", not just "202".
Moved from audit A (`a-repro`): A-01 ON_PREM, string `env_file_ref`, real pipeline input.
"""

import base64
import json
from pathlib import Path

import pytest
from cmp_api.config import settings
from cmp_api.connections import EnvironmentConnection
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import LocalArtifactStore
from cmp_api.plan_inputs import register_planning_input
from cmp_api.routes import migrations as migration_routes
from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.provisioning import ContentRef, VersionRef
from cmp_contracts.provisioning_v2 import RequirementsV2, S3ArchiveRef
from cmp_contracts.verification import BuildAttempt, canonical_requirements
from cmp_providers.env_file_store import EncryptedEnvFileStore, load_key
from cmp_worker.plan_publish import PlanPublisher
from cmp_worker.planning_handoff import PlanningSettings, assemble_registration
from cmp_worker.start_input import build_start_input
from fastapi.testclient import TestClient
from onprem_rollout_support import register_agent
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_aws_preview import request as aws_request
from test_gcp_preview import request as gcp_request

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "tests/fixtures/analysis"
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
DB_IMAGE = "docker.io/library/postgres@sha256:" + "e" * 64
DIGEST = "sha256:" + "b" * 64
COMMIT = "a" * 40
SOURCE = {
    "kind": "GITHUB_PUBLIC",
    "repository_id": 123,
    "full_name": "team/demo-app",
    "commit_sha": COMMIT,
}
WORKLOADS = {"namespace": "cmp-demo", "storage_class": "gp3", "database_image": DB_IMAGE}


def cloud_settings(model):
    data = model.model_dump(mode="json")
    del data["migration_id"], data["plan"]
    return data


PROFILES = {
    "profiles": {
        "onprem-k3s-basic": {
            "provider": "ON_PREM",
            "namespace": "cmp-demo",
            "storage_class": "local-path",
            "database_image": DB_IMAGE,
        },
        "gcp-vm-basic": {
            "provider": "GCP",
            "settings": cloud_settings(gcp_request()),
            "workloads": {**WORKLOADS, "storage_class": "pd-balanced"},
        },
        "aws-eks-basic": {
            "provider": "AWS",
            "settings": cloud_settings(aws_request()),
            "workloads": WORKLOADS,
        },
    }
}


class FakeTemporal:
    def __init__(self):
        self.started = []

    async def start_workflow(self, name, data, **kwargs):
        self.started.append({"data": data, "id": kwargs["id"]})

        class Handle:
            result_run_id = "run-1"

        return Handle()


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def env_store(tmp_path):
    return EncryptedEnvFileStore(tmp_path / "env-files", load_key(KEY))


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "target-profiles.json"
    path.write_text(json.dumps(PROFILES))
    return path


@pytest.fixture
def api(db, env_store, catalog, monkeypatch):
    fake = FakeTemporal()

    async def get_client():
        return fake

    monkeypatch.setattr(migration_routes, "get_temporal_client", get_client)
    monkeypatch.setattr(settings, "planning_target_profiles_file", str(catalog))
    app.dependency_overrides[migration_routes.get_env_file_store_factory] = lambda: (
        lambda: env_store
    )
    app.dependency_overrides[migration_routes.get_session_factory] = lambda: db
    yield TestClient(app), fake
    app.dependency_overrides.clear()


@pytest.fixture
def env_ref(env_store) -> ContentRef:
    ref, _, _ = env_store.put(b"DB_PASSWORD=never-shown\n")
    return ref


@pytest.fixture
def agent(db):
    with db() as session:
        register_agent(session, agent_id="office-seoul-01", cluster_uid="cluster-seoul")


def add_connection(db, kind, *, status="CONNECTED", **fields) -> str:
    connection_id = "conn-" + ("1" if kind == "AWS_TARGET" else "2") * 32
    with db() as session:
        session.add(
            EnvironmentConnection(
                id=connection_id,
                kind=kind,
                display_name="target",
                region="ap-northeast-2",
                status=status,
                created_at=1,
                **fields,
            )
        )
        session.commit()
    return connection_id


def web_request(target, env_file_ref, **extra):
    """What the web BFF sends (`sseProvider`): legacy top-level fields + `StartRequest`."""
    return {
        "project_id": "demo",
        "source_provider": "AWS",
        "target_provider": target["provider"],
        "target": target,
        "source": SOURCE,
        "env_file_ref": env_file_ref,
        **extra,
    }


ONPREM = {"provider": "ON_PREM", "profile": "onprem-k3s-basic", "agent_id": "office-seoul-01"}


# --- A-01: accepted, nothing dropped --------------------------------------------------------


def test_web_on_prem_start_is_accepted_and_reaches_the_real_pipeline(api, env_ref, agent):
    client, temporal = api
    response = client.post("/migrations", json=web_request(ONPREM, env_ref.id))
    assert response.status_code == 202, response.text
    data = temporal.started[0]["data"]
    assert set(data) == {"project_id", "analysis", "sandbox", "planning", "env_file_ref"}
    assert data["analysis"]["repository"] == "https://github.com/team/demo-app.git"
    assert data["analysis"]["requested_commit"] == COMMIT
    # The string reference is pinned to the stored version and sha at start (S5).
    assert data["env_file_ref"] == env_ref.model_dump(mode="json")
    target = data["planning"]["target"]
    assert target == {
        "provider": "ON_PREM",
        "environment_id": "office-seoul-01",
        "agent_id": "office-seoul-01",
        "cluster_uid": "cluster-seoul",
        "namespace": "cmp-demo",
        "storage_class": "local-path",
        "database_image": DB_IMAGE,
    }
    selection = data["planning"]["target_selection"]
    assert selection["provider"] == "ON_PREM" and selection["location"] is None
    assert selection["access_ref"] == {"id": "office-seoul-01", "version": "cluster-seoul"}
    assert selection["profile"] == "onprem-k3s-basic"
    assert data["planning"]["env_file_ref"] is None  # the parent copies the start reference in


def test_env_file_ref_string_resolves_to_the_latest_version(api, env_store, env_ref, agent):
    client, temporal = api
    newer, _, _ = env_store.put(b"DB_PASSWORD=changed\n", file_id=env_ref.id)
    assert client.post("/migrations", json=web_request(ONPREM, env_ref.id)).status_code == 202
    assert temporal.started[0]["data"]["env_file_ref"] == newer.model_dump(mode="json")
    # An object reference keeps meaning exactly that version.
    exact = web_request(ONPREM, env_ref.model_dump(mode="json"))
    assert client.post("/migrations", json=exact).status_code == 202
    assert temporal.started[1]["data"]["env_file_ref"] == env_ref.model_dump(mode="json")


def test_gcp_start_uses_the_connection_and_requested_region(api, db, env_ref):
    client, temporal = api
    connection = add_connection(db, "GCP_TARGET", project_id="sbh26-test")
    target = {
        "provider": "GCP",
        "profile": "gcp-vm-basic",
        "region": "asia-northeast1",
        "credential_ref": connection,
    }
    assert client.post("/migrations", json=web_request(target, env_ref.id)).status_code == 202
    planning = temporal.started[0]["data"]["planning"]
    gcp = planning["target"]["settings"]
    assert gcp["region"] == "asia-northeast1" and gcp["project_id"] == "sbh26-test"
    assert gcp["credential_ref"] == {"id": connection, "version": "1"}
    assert planning["target"]["workloads"]["storage_class"] == "pd-balanced"
    selection = planning["target_selection"]
    assert selection["location"] == "asia-northeast1"
    assert selection["access_ref"] == gcp["credential_ref"]


def test_connection_values_that_break_the_profile_are_refused(api, db, env_ref):
    client, temporal = api
    # The profile's node service account belongs to its own project (06 P0 rule).
    connection = add_connection(db, "GCP_TARGET", project_id="other-project")
    target = {"provider": "GCP", "profile": "gcp-vm-basic", "credential_ref": connection}
    response = client.post("/migrations", json=web_request(target, env_ref.id))
    assert code_of(response) == "TARGET_SETTINGS_INVALID" and temporal.started == []


def test_aws_start_takes_the_connection_profile_and_account(api, db, env_ref):
    client, temporal = api
    connection = add_connection(
        db, "AWS_TARGET", role_arn="arn:aws:iam::222222222222:role/cmp-target",
        account_id="222222222222",
    )  # fmt: skip
    profile_name = "cmp-conn-" + connection.removeprefix("conn-")
    target = {"provider": "AWS", "profile": "aws-eks-basic", "credential_ref": profile_name}
    response = client.post("/migrations", json=web_request(target, env_ref.id))
    assert response.status_code == 202, response.text
    aws = temporal.started[0]["data"]["planning"]["target"]["settings"]
    assert aws["credential_ref"] == {"id": profile_name, "version": "1"}
    assert aws["account_id"] == "222222222222"
    assert aws["region"] == aws_request().region  # profile default when not requested


def test_without_a_connection_the_profile_credential_is_used(api, env_ref):
    client, temporal = api
    target = {"provider": "GCP", "profile": "gcp-vm-basic"}
    assert client.post("/migrations", json=web_request(target, env_ref.id)).status_code == 202
    gcp = temporal.started[0]["data"]["planning"]["target"]["settings"]
    assert gcp == cloud_settings(gcp_request())


# --- refusals: codes, nothing started ------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"unknown": 1},
        {"target": {**ONPREM, "unknown": 1}},
        {"source": {**SOURCE, "default_branch": "main"}},
    ],
)
def test_unknown_fields_are_refused(api, env_ref, agent, body):
    client, temporal = api
    request = web_request(ONPREM, env_ref.id)
    for key, value in body.items():
        request[key] = value
    assert client.post("/migrations", json=request).status_code == 422
    assert temporal.started == []


def code_of(response):
    return response.json()["detail"]["code"]


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"env_file_ref": None}, 422, "ENV_FILE_REF_REQUIRED"),
        ({"env_file_ref": "envf-" + "0" * 32}, 422, "ENV_FILE_NOT_FOUND"),
        ({"source": None}, 422, "SOURCE_REQUIRED"),
        ({"target_provider": "GCP"}, 422, "TARGET_PROVIDER_MISMATCH"),
        ({"target": {**ONPREM, "profile": "nope"}}, 422, "TARGET_PROFILE_NOT_FOUND"),
        (
            {"target": {**ONPREM, "profile": "gcp-vm-basic"}},
            422,
            "TARGET_PROFILE_PROVIDER_MISMATCH",
        ),
        ({"target": {**ONPREM, "agent_id": None}}, 422, "TARGET_AGENT_REQUIRED"),
        ({"target": {**ONPREM, "agent_id": "ghost"}}, 422, "TARGET_AGENT_NOT_FOUND"),
        ({"target": {**ONPREM, "region": "x"}}, 422, "TARGET_FIELD_NOT_APPLICABLE"),
        ({"target": {**ONPREM, "credential_ref": "conn-1"}}, 422, "TARGET_FIELD_NOT_APPLICABLE"),
        (
            {"target": {"provider": "GCP", "profile": "gcp-vm-basic", "agent_id": "a"}},
            422,
            "TARGET_FIELD_NOT_APPLICABLE",
        ),
        (
            {"target": {"provider": "GCP", "profile": "gcp-vm-basic", "region": "Bad Region"}},
            422,
            "TARGET_SETTINGS_INVALID",
        ),
        (
            {"target": {"provider": "GCP", "profile": "gcp-vm-basic", "credential_ref": "conn-x"}},
            422,
            "TARGET_CONNECTION_NOT_FOUND",
        ),
    ],
)
def test_refusals_have_codes_and_start_nothing(api, env_ref, agent, change, status, code):
    client, temporal = api
    request = web_request(change.get("target", ONPREM), env_ref.id)
    request = {**request, **change}
    request = {key: value for key, value in request.items() if value is not None}
    response = client.post("/migrations", json=request)
    assert (response.status_code, code_of(response)) == (status, code)
    assert temporal.started == []
    assert "never-shown" not in response.text


def test_connection_must_match_the_target_and_be_connected(api, db, env_ref):
    client, _ = api
    aws = add_connection(db, "AWS_TARGET", status="PENDING")
    target = {"provider": "GCP", "profile": "gcp-vm-basic", "credential_ref": aws}
    response = client.post("/migrations", json=web_request(target, env_ref.id))
    assert code_of(response) == "TARGET_CONNECTION_KIND_MISMATCH"
    target = {"provider": "AWS", "profile": "aws-eks-basic", "credential_ref": aws}
    response = client.post("/migrations", json=web_request(target, env_ref.id))
    assert code_of(response) == "TARGET_CONNECTION_NOT_READY"


def test_revoked_agent_is_refused(api, db, env_ref, agent):
    from cmp_api.agent_registry import revoke_agent

    with db() as session:
        revoke_agent(session, agent_id="office-seoul-01")
    response = api[0].post("/migrations", json=web_request(ONPREM, env_ref.id))
    assert code_of(response) == "TARGET_AGENT_NOT_FOUND"


def test_cloud_target_needs_a_region(api, env_ref, catalog):
    client, _ = api
    profiles = json.loads(catalog.read_text())
    del profiles["profiles"]["gcp-vm-basic"]["settings"]["region"]
    catalog.write_text(json.dumps(profiles))
    target = {"provider": "GCP", "profile": "gcp-vm-basic"}
    response = client.post("/migrations", json=web_request(target, env_ref.id))
    assert code_of(response) == "TARGET_REGION_REQUIRED"


def test_without_a_catalog_the_real_start_is_unavailable(api, env_ref, agent, monkeypatch):
    monkeypatch.setattr(settings, "planning_target_profiles_file", None)
    response = api[0].post("/migrations", json=web_request(ONPREM, env_ref.id))
    assert (response.status_code, code_of(response)) == (503, "TARGET_PROFILES_UNCONFIGURED")


def test_legacy_start_without_target_is_unchanged(api):
    client, temporal = api
    assert client.post("/migrations", json={"project_id": "demo"}).status_code == 202
    assert temporal.started[0]["data"] == "demo"


# --- the produced input is the CLI input and feeds Planning ---------------------------------


def test_the_start_input_equals_the_cli_start_input(api, env_ref, agent):
    client, temporal = api
    client.post("/migrations", json=web_request(ONPREM, env_ref.id))
    data = temporal.started[0]["data"]
    cli = build_start_input(
        project_id="demo",
        repository=data["analysis"]["repository"],
        commit=COMMIT,
        planning_json=json.dumps(data["planning"]),
        env_file_ref_json=json.dumps(data["env_file_ref"]),
    )
    assert data == cli


def registration_for(planning: dict, env_ref: ContentRef):
    """What `sbx_plan_assemble` does with the start input (parent copies `env_file_ref` in)."""
    data = json.loads((ANALYSIS / "analysis-report-eks-k3s.json").read_text(encoding="utf-8"))
    report = AnalysisReport.model_validate(data["report"])
    requirements = RequirementsV2.model_validate_json(
        (ANALYSIS / "requirements-eks-k3s.json").read_bytes()
    )
    content, sha = canonical_requirements(requirements)
    src, suite = "a" * 64, "d" * 64
    source = S3ArchiveRef(uri=f"s3://cmp-sources/sources/{src}.tar.gz", sha256=src)
    builds = [
        BuildAttempt(
            migration_id=report.migration_id,
            resource_id=resource,
            attempt_id=f"{resource}-b1",
            result_ref=VersionRef(id=f"build-{resource}", version="1"),
            status="SUCCEEDED",
            source_ref=source,
            platform="linux/amd64",
            image_uri=f"registry.example/{resource}@{DIGEST}",
            image_digest=DIGEST,
        )
        for resource in ("app",)
    ]
    settings_ = PlanningSettings.model_validate(
        {**planning, "env_file_ref": env_ref.model_dump(mode="json")}
    )
    registration = assemble_registration(
        report=report,
        requirements_ref=ContentRef(id="requirements", version="1", sha256=sha),
        report_ref=ContentRef(id="report", version="1", sha256="c" * 64),
        builds=builds,
        attempts=[{"attempt": 1, "passed": True}],
        suite_ref=S3ArchiveRef(uri=f"s3://cmp-sources/tests/{suite}.tar.gz", sha256=suite),
        mapping_ref=ContentRef(id="mapping", version="1", sha256="f" * 64),
        settings=settings_,
    )
    return report.migration_id, registration.model_copy(
        update={"requirements_json": content.decode()}
    )


@pytest.fixture
def platform_registry(monkeypatch):
    """Ticket 29: ON_PREM jobs need the platform ECR configured."""
    monkeypatch.setattr(
        settings, "platform_ecr_registry", "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
    )
    monkeypatch.setattr(settings, "platform_ecr_repositories", "demo")


def test_web_on_prem_start_reaches_plan_publication(
    api, db, env_ref, agent, tmp_path, platform_registry
):
    client, temporal = api
    client.post("/migrations", json=web_request(ONPREM, env_ref.id))
    data = temporal.started[0]["data"]
    migration_id, registration = registration_for(data["planning"], env_ref)
    store = LocalArtifactStore(tmp_path / "store")
    with db() as session:
        register_planning_input(session, migration_id, registration, store)
    result = PlanPublisher(db, store, cloud_artifact_root=tmp_path / "plans").publish(migration_id)
    assert (result["status"], result["code"]) == ("PUBLISHED", "PLAN_PUBLISHED"), result
    assert (result["provider"], result["scope"]) == ("ON_PREM", "WORKLOADS")


@pytest.mark.parametrize(
    "target",
    [
        {"provider": "GCP", "profile": "gcp-vm-basic", "region": "asia-northeast3"},
        {"provider": "AWS", "profile": "aws-eks-basic"},
    ],
)
def test_cloud_start_is_a_valid_planning_registration(api, db, env_ref, tmp_path, target):
    client, temporal = api
    assert client.post("/migrations", json=web_request(target, env_ref.id)).status_code == 202
    migration_id, registration = registration_for(temporal.started[0]["data"]["planning"], env_ref)
    with db() as session:
        digest = register_planning_input(
            session, migration_id, registration, LocalArtifactStore(tmp_path / "store")
        )
    assert digest


def test_the_demo_example_catalog_is_valid():
    """`infra/demo/target-profiles.example.json` builds valid settings for every profile."""
    from types import SimpleNamespace

    from cmp_api.start_request import load_target_profiles, planning_settings
    from cmp_contracts.migration import StartTarget

    path = ROOT / "infra/demo/target-profiles.example.json"
    profiles = load_target_profiles(str(path))
    assert set(profiles) == {"onprem-k3s-basic", "gcp-vm-basic", "aws-eks-basic"}
    agent = SimpleNamespace(id="agent-a", cluster_uid="cluster-a")
    for name, entry in profiles.items():
        target = StartTarget(provider=entry["provider"], profile=name)
        planning = planning_settings(target, entry, agent=agent)
        assert PlanningSettings.model_validate(planning).target.provider == entry["provider"]
